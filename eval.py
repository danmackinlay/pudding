"""Single-cell audition runner (ASYNC) — grade one (model × strategy × provider) over a set.

Concurrency is asyncio, not threads: the work is IO-bound (provider HTTP), so k samples and
many problems run concurrently on one event loop, bounded by a Semaphore (--concurrency) to
respect provider rate limits. Per-problem timeouts use asyncio.wait_for (cancellable — no
orphaned-thread hangs). tir_fence's blocking kernel is the only thing offloaded to a thread
(inside solve_one_async). `audition.py` sweeps a matrix of cells; this is one cell.

Instrumentation (to localise slowness): per-item TTFT and decode tok/s, plus per-token cost
and maj@k agreement, are reported per-item and aggregated.

    eval.py --provider featherless --model Qwen/Qwen2.5-Math-72B-Instruct --strategy tir_fence --data aime24 --k 8
    eval.py --provider openrouter --model moonshotai/kimi-k2.6 --strategy cot --data aime24 --k 8 --concurrency 8
    eval.py --provider openrouter --model qwen/qwen3.7-max --strategy self_verify --data amc23 --k 4

Integer-answer sets grade with a normalized `==`; MATH-500 is LaTeX → swap in `math_verify`.
"""
import argparse
import asyncio
import json
import time
from collections import Counter

from solver_loop import solve_one_async

try:                                              # optional — only MATH-500 needs it
    from math_verify import parse as _mv_parse, verify as _mv_verify
    _HAS_MATH_VERIFY = True
except Exception:
    _HAS_MATH_VERIFY = False

GENERALIST = {"cot", "self_verify", "tools"}      # need an explicit --model; want a fat budget


# --- data loaders ----------------------------------------------------------
def load_samples(path: str = "samples.jsonl") -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_gsm8k(n: int = 20) -> list[dict]:
    """First n GSM8K test items. Gold answer is the integer after the '####' line."""
    from datasets import load_dataset
    ds = load_dataset("openai/gsm8k", "main", split="test")
    out = []
    for row in ds.select(range(min(n, len(ds)))):
        gold = row["answer"].split("####")[-1].strip().replace(",", "")
        out.append({"problem": row["question"], "answer": gold})
    return out


def _load_hf(name: str, n: int, problem_key: str = "problem", answer_key: str = "answer",
             split: str | None = None, config: str | None = None) -> list[dict]:
    """Generic HF loader: pull `n` rows, project to {problem, answer}. Split auto-detected
    (prefers 'test') and column names overridable — verify keys on first run if it KeyErrors."""
    from datasets import load_dataset, get_dataset_split_names
    if split is None:
        try:
            splits = get_dataset_split_names(name, config) if config else get_dataset_split_names(name)
            split = "test" if "test" in splits else (splits[0] if splits else "train")
        except Exception:
            split = "train"
    ds = load_dataset(name, config, split=split) if config else load_dataset(name, split=split)
    return [{"problem": str(r[problem_key]), "answer": str(r[answer_key]).strip()}
            for r in ds.select(range(min(n, len(ds))))]


def _level(r) -> int:
    lv = r.get("level")
    if isinstance(lv, (int, float)):
        return int(lv)
    try:
        return int(str(lv).strip().split()[-1])      # "Level 5" → 5
    except Exception:
        return 0


def load_math500(n: int = 20, min_level: int | None = None) -> list[dict]:
    """First n MATH-500 items (optionally filtered to level >= min_level — 5 is the hardest
    tier). Answers are LaTeX → graded by math_verify (symbolic equivalence)."""
    from datasets import load_dataset
    rows = list(load_dataset("HuggingFaceH4/MATH-500", split="test"))
    if min_level is not None:
        rows = [r for r in rows if _level(r) >= min_level]
    return [{"problem": r["problem"], "answer": r["answer"]} for r in rows[:n]]


def load_amc23(n: int = 40) -> list[dict]:
    return _load_hf("AI-MO/aimo-validation-amc", n)


def load_aime24(n: int = 30) -> list[dict]:
    return _load_hf("AI-MO/aimo-validation-aime", n)


def load_data(name: str, n: int) -> list[dict]:
    return {
        "samples": lambda: load_samples(),
        "gsm8k": lambda: load_gsm8k(n),
        "math500": lambda: load_math500(n),
        "math500_hard": lambda: load_math500(n, min_level=5),   # hardest tier (level 5)
        "amc23": lambda: load_amc23(n),
        "aime24": lambda: load_aime24(n),
    }.get(name, lambda: load_samples(name))()


# --- grading ---------------------------------------------------------------
def _as_number(s: str):
    for cast in (int, float):
        try:
            return cast(s.replace(",", "").strip())
        except (ValueError, AttributeError):
            continue
    return None


def grade(pred: str | None, gold: str) -> bool:
    """True if pred matches gold. **Exact string match first** so identical answers — including
    symbolic ones like 'p - q' that math_verify can't compare — are never failed by the grader
    (a real bug the calibration caught). Then integer/decimal equality (AIME/GSM8K/AMC); then
    LaTeX symbolic equivalence via math_verify (MATH-500: \\frac{1}{2} == 0.5 == 0.50)."""
    if pred is None:
        return False
    if pred.strip() == gold.strip():
        return True
    pn, gn = _as_number(pred), _as_number(gold)
    if pn is not None and gn is not None:
        return pn == gn
    if _HAS_MATH_VERIFY:
        try:
            return bool(_mv_verify(_mv_parse(gold), _mv_parse(pred)))   # verify(gold, pred)
        except Exception:
            pass
    return False


def _mean(xs: list) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# --- run (async) -----------------------------------------------------------
async def solve_graded_async(problem: str, k: int, model: str | None, *, strategy: str = "tir_fence",
                             provider: str | None = None, max_tokens: int | None = None,
                             max_calls: int | None = None, sem: asyncio.Semaphore | None = None) -> dict:
    """k==1 (greedy) or maj@k (k concurrent seeded chains) → {pred, tokens, agreement, ttft,
    decode, truncated, error}. The Semaphore bounds total concurrent provider calls."""
    budget = {}
    if max_tokens is not None:
        budget["max_tokens"] = max_tokens
    if max_calls is not None:
        budget["max_calls"] = max_calls

    async def one(seed=None, temperature=0.0) -> dict:
        async def go():
            return await solve_one_async(problem, strategy=strategy, model=model, provider=provider,
                                         temperature=temperature, seed=seed, **budget)
        if sem is None:
            return await go()
        async with sem:
            return await go()

    if k <= 1:
        r = await one()
        return {"pred": r["boxed"], "tokens": r["completion_tokens"], "agreement": None,
                "ttft": r.get("ttft_s"), "decode": r.get("decode_tok_s"),
                "truncated": r["truncated"], "error": r.get("error")}

    rs = await asyncio.gather(*[one(seed=s, temperature=0.6) for s in range(k)])
    tokens = sum(r["completion_tokens"] for r in rs)
    ttfts = [r["ttft_s"] for r in rs if r.get("ttft_s") is not None]
    decs = [r["decode_tok_s"] for r in rs if r.get("decode_tok_s") is not None]
    ttft = min(ttfts) if ttfts else None
    decode = _mean(decs) if decs else None
    truncated = any(r["truncated"] for r in rs)
    err = next((r["error"] for r in rs if r.get("error")), None)
    answers = [r["boxed"] for r in rs if r["boxed"]]
    if not answers:
        return {"pred": None, "tokens": tokens, "agreement": 0.0, "ttft": ttft,
                "decode": decode, "truncated": truncated, "error": err}
    winner, count = Counter(answers).most_common(1)[0]
    return {"pred": winner, "tokens": tokens, "agreement": count / len(answers),
            "ttft": ttft, "decode": decode, "truncated": truncated, "error": err}


async def evaluate(problems: list[dict], k: int = 1, model: str | None = None, *,
                   strategy: str = "tir_fence", provider: str | None = None,
                   max_tokens: int | None = None, max_calls: int | None = None,
                   timeout: float | None = None, concurrency: int = 6,
                   verbose: bool = True) -> dict:
    """Grade `problems` concurrently (bounded by `concurrency`); print per-item lines as they
    land + a summary; return a metrics dict (acc, cost, agreement, TTFT, decode rate)."""
    sem = asyncio.Semaphore(concurrency)
    counts = {"ok": 0, "wrong": 0, "no_answer": 0, "timeout": 0, "error": 0}
    times, toks, agrees, ttfts, decs = [], [], [], [], []

    async def graded(idx: int, item: dict):
        t0 = time.perf_counter()
        status = None
        try:
            coro = solve_graded_async(item["problem"], k, model, strategy=strategy,
                                      provider=provider, max_tokens=max_tokens,
                                      max_calls=max_calls, sem=sem)
            res = await (asyncio.wait_for(coro, timeout) if timeout else coro)
        except asyncio.TimeoutError:
            res, status = {"pred": None, "tokens": 0, "agreement": None, "ttft": None,
                           "decode": None}, "timeout"
        except Exception as e:  # noqa: BLE001
            res, status = {"pred": f"{type(e).__name__}: {e}"[:40], "tokens": 0,
                           "agreement": None, "ttft": None, "decode": None}, "error"
        return idx, item, res, status, time.perf_counter() - t0

    sweep_t0 = time.perf_counter()
    tasks = [asyncio.create_task(graded(i, it)) for i, it in enumerate(problems, 1)]
    for fut in asyncio.as_completed(tasks):
        idx, item, res, status, elapsed = await fut
        times.append(elapsed)
        toks.append(res.get("tokens") or 0)
        if k > 1 and res.get("agreement") is not None:
            agrees.append(res["agreement"])
        if res.get("ttft") is not None:
            ttfts.append(res["ttft"])
        if res.get("decode") is not None:
            decs.append(res["decode"])
        pred = res.get("pred")
        if status is None:
            status = "ok" if grade(pred, item["answer"]) else \
                ("no_answer" if pred in (None, "") else "wrong")
        counts[status] += 1
        if verbose:
            mark = {"ok": "✓", "wrong": "✗", "no_answer": "∅", "timeout": "⏱", "error": "!"}[status]
            ttft = res.get("ttft")
            dec = res.get("decode")
            ttft_s = f"{ttft:4.1f}s" if ttft is not None else "  — "
            dec_s = f"{dec:4.0f}t/s" if dec is not None else "  —  "
            prob = item["problem"][:42] + ("…" if len(item["problem"]) > 42 else "")
            print(f"{mark} [{idx:>3}/{len(problems)}] {status:<9} {elapsed:5.1f}s "
                  f"ttft {ttft_s} {dec_s} {res.get('tokens') or 0:>6}tok "
                  f"pred={str(pred):<10} gold={item['answer']:<8} {prob}", flush=True)

    n = len(problems)
    acc = counts["ok"] / n if n else 0.0
    wall = time.perf_counter() - sweep_t0
    mean_agreement = _mean(agrees) if agrees else None
    metrics = {"acc": acc, "n": n, "counts": counts, "wall": wall, "mean_time": _mean(times),
               "mean_tokens": _mean(toks), "total_tokens": sum(toks),
               "mean_agreement": mean_agreement,
               "mean_ttft": _mean(ttfts) if ttfts else None,
               "mean_decode": _mean(decs) if decs else None}
    tag = f"maj@{k}" if k > 1 else "greedy"
    agree_str = f" | agreement {mean_agreement:.0%} mean" if mean_agreement is not None else ""
    ttft_str = f"{metrics['mean_ttft']:.1f}s" if metrics["mean_ttft"] is not None else "n/a"
    dec_str = f"{metrics['mean_decode']:.0f} tok/s" if metrics["mean_decode"] is not None else "n/a"
    print("=" * 72)
    print(f"accuracy ({tag}): {counts['ok']}/{n} = {acc:.1%}   "
          f"| wrong={counts['wrong']} no_answer={counts['no_answer']} "
          f"timeout={counts['timeout']} error={counts['error']}")
    print(f"cost: {metrics['mean_tokens']:.0f} tok/problem mean, {metrics['total_tokens']} total"
          f"{agree_str}")
    print(f"speed: {wall:.0f}s wall (concurrency {concurrency}) | {metrics['mean_time']:.1f}s/problem "
          f"mean | TTFT {ttft_str} | decode {dec_str}")
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="samples",
                    help="samples | gsm8k | math500 | amc23 | aime24 | <path.jsonl>")
    ap.add_argument("--n", type=int, default=20, help="how many items for the HF benchmarks")
    ap.add_argument("--k", type=int, default=1, help="maj@k (k>1 votes k concurrent seeded chains)")
    ap.add_argument("--strategy", default="tir_fence",
                    choices=["tir_fence", "cot", "self_verify", "tools"])
    ap.add_argument("--provider", default=None,
                    help="featherless | openrouter | novita | moonshot | deepinfra | ollama | selfhost")
    ap.add_argument("--model", default=None, help="model id (required for generalist strategies)")
    ap.add_argument("--max-tokens", type=int, default=None, help="per-round budget")
    ap.add_argument("--max-calls", type=int, default=None, help="max TIR rounds")
    ap.add_argument("--timeout", type=float, default=None, help="per-problem wall-clock seconds")
    ap.add_argument("--concurrency", type=int, default=6,
                    help="max in-flight problems/samples (bound to the provider's limit)")
    args = ap.parse_args()

    if args.strategy in GENERALIST and not args.model:
        ap.error(f"--strategy {args.strategy} needs --model (the audition names the generalist)")
    if args.max_tokens is None and args.strategy in GENERALIST:
        args.max_tokens = 16384      # thinking shares the budget; a stingy cap empties \boxed{}

    problems = load_data(args.data, args.n)
    print(f"audition: provider={args.provider or '(default)'} model={args.model or '(default)'} "
          f"strategy={args.strategy} data={args.data} n={len(problems)} k={args.k} "
          f"concurrency={args.concurrency}")
    asyncio.run(evaluate(problems, k=args.k, model=args.model, strategy=args.strategy,
                         provider=args.provider, max_tokens=args.max_tokens,
                         max_calls=args.max_calls, timeout=args.timeout,
                         concurrency=args.concurrency))
