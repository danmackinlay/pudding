"""Single-cell audition runner — grade one (model × strategy × provider) over a problem set.

Reports accuracy, **per-token cost** (the $ proxy; flat-rate providers still read wall-time),
and for maj@k the **agreement margin** (the confidence signal). This is one cell; `audition.py`
sweeps a matrix of cells and tabulates. Hold the problem set + grader constant, vary the
contender (PLAN.md §3).

    # incumbent specialist (metered tokens, local kernel)
    eval.py --provider featherless --model nvidia/OpenMath-Nemotron-32B --strategy tir_fence --data aime24 --k 8
    # generalist, pure chain-of-thought (rung 1), no executor
    eval.py --provider openrouter --model moonshotai/kimi-k2.6 --strategy cot --data aime24 --k 8
    # generalist, self-verification (rung 2)
    eval.py --provider openrouter --model qwen/qwen3-235b-a22b-thinking --strategy self_verify --data amc23 --k 4

Integer-answer sets (staircase, GSM8K, AMC, AIME) grade with a normalized `==`. MATH-500 is
LaTeX → needs symbolic equality; the `math_verify` hook in grade() is the swap point. Start on
integer sets so a failure points at the loop, not the grader.
"""
import argparse
import json
from collections import Counter

from solver_loop import Kernel, solve_one

# Rungs whose orchestrator drives a code executor; the rest (cot/self_verify) are executor-free.
NEEDS_EXECUTOR = {"tir_fence", "tools"}
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


def load_math500(n: int = 20) -> list[dict]:
    """First n MATH-500 items. NB answers are LaTeX → needs the symbolic grader."""
    return _load_hf("HuggingFaceH4/MATH-500", n, split="test")


def load_amc23(n: int = 40) -> list[dict]:
    """AMC-style, integer answers — gradeable with `==`."""
    return _load_hf("AI-MO/aimo-validation-amc", n)


def load_aime24(n: int = 30) -> list[dict]:
    """AIME, integer answers 0–999 — the headline number, gradeable with `==`."""
    return _load_hf("AI-MO/aimo-validation-aime", n)


def load_data(name: str, n: int) -> list[dict]:
    return {
        "samples": lambda: load_samples(),
        "gsm8k": lambda: load_gsm8k(n),
        "math500": lambda: load_math500(n),
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
    """Integer/decimal-aware equality. Good for GSM8K/AMC/AIME/staircase.

    MATH is LaTeX (\\frac{1}{2} == 0.5 == 0.50) — swap in a symbolic checker there:
        from math_verify import parse, verify
        return verify(parse(gold), parse(pred))
    """
    if pred is None:
        return False
    pn, gn = _as_number(pred), _as_number(gold)
    if pn is not None and gn is not None:
        return pn == gn
    return pred.strip() == gold.strip()


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


# --- run -------------------------------------------------------------------
def solve_graded(problem: str, k: int, model: str | None, *, strategy: str = "tir_fence",
                 provider: str | None = None, max_tokens: int | None = None,
                 max_calls: int | None = None) -> dict:
    """Greedy single shot (k==1) or local maj@k (k>1) → {pred, tokens, agreement, truncated}.

    `tokens` is the summed completion-token cost over all k chains; `agreement` is the winning
    vote fraction (the confidence signal; None at k==1). Only tir_fence/tools get a Kernel.
    """
    budget = {}
    if max_tokens is not None:
        budget["max_tokens"] = max_tokens
    if max_calls is not None:
        budget["max_calls"] = max_calls
    needs = strategy in NEEDS_EXECUTOR

    def one(seed=None, temperature=0.0) -> dict:
        kern = Kernel() if needs else None
        try:
            return solve_one(problem, executor=kern, model=model, provider=provider,
                             strategy=strategy, temperature=temperature, seed=seed, **budget)
        finally:
            if kern is not None:
                kern.km.shutdown_kernel(now=True)

    if k <= 1:
        r = one()
        return {"pred": r["boxed"], "tokens": r["completion_tokens"],
                "agreement": None, "truncated": r["truncated"]}

    results = [one(seed=s, temperature=0.6) for s in range(k)]
    tokens = sum(r["completion_tokens"] for r in results)
    truncated = any(r["truncated"] for r in results)
    answers = [r["boxed"] for r in results if r["boxed"]]
    if not answers:
        return {"pred": None, "tokens": tokens, "agreement": 0.0, "truncated": truncated}
    winner, count = Counter(answers).most_common(1)[0]
    return {"pred": winner, "tokens": tokens, "agreement": count / len(answers),
            "truncated": truncated}


def evaluate(problems: list[dict], k: int = 1, model: str | None = None, *,
             strategy: str = "tir_fence", provider: str | None = None,
             max_tokens: int | None = None, max_calls: int | None = None,
             timeout: float | None = None, verbose: bool = True) -> dict:
    """Grade `problems`; print a status breakdown + cost/agreement; return a metrics dict.

    Returns {acc, n, counts, mean_time, mean_tokens, total_tokens, mean_agreement} so the
    matrix runner (audition.py) can tabulate. `verbose=False` suppresses per-item lines.
    A per-problem wall-clock `timeout` keeps a runaway from stalling the sweep.
    """
    import concurrent.futures as cf
    import time

    counts = {"ok": 0, "wrong": 0, "no_answer": 0, "timeout": 0, "error": 0}
    times: list[float] = []
    toks: list[int] = []
    agrees: list[float] = []
    sweep_t0 = time.time()
    for i, item in enumerate(problems, 1):
        res, status, t0 = None, None, time.time()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(solve_graded, item["problem"], k, model, strategy=strategy,
                            provider=provider, max_tokens=max_tokens, max_calls=max_calls)
            try:
                res = fut.result(timeout=timeout)
            except cf.TimeoutError:
                status = "timeout"
            except Exception as e:  # noqa: BLE001 — surface, don't crash the sweep
                status = "error"
                res = {"pred": f"{type(e).__name__}: {e}"[:40], "tokens": 0, "agreement": None}
        elapsed = time.time() - t0
        times.append(elapsed)
        res = res or {"pred": None, "tokens": 0, "agreement": None}
        pred = res.get("pred")
        toks.append(res.get("tokens") or 0)
        if k > 1 and res.get("agreement") is not None:
            agrees.append(res["agreement"])
        if status is None:
            if grade(pred, item["answer"]):
                status = "ok"
            elif pred in (None, ""):
                status = "no_answer"
            else:
                status = "wrong"
        counts[status] += 1
        if verbose:
            mark = {"ok": "✓", "wrong": "✗", "no_answer": "∅", "timeout": "⏱", "error": "!"}[status]
            prob = item["problem"][:48] + ("…" if len(item["problem"]) > 48 else "")
            print(f"{mark} [{i:>3}/{len(problems)}] {status:<9} {elapsed:5.1f}s "
                  f"{res.get('tokens') or 0:>6}tok pred={str(pred):<10} "
                  f"gold={item['answer']:<8} {prob}", flush=True)

    n = len(problems)
    acc = counts["ok"] / n if n else 0.0
    tag = f"maj@{k}" if k > 1 else "greedy"
    mean_agreement = _mean(agrees) if agrees else None
    metrics = {"acc": acc, "n": n, "counts": counts, "mean_time": _mean(times),
               "mean_tokens": _mean(toks), "total_tokens": sum(toks),
               "mean_agreement": mean_agreement}
    print("=" * 72)
    print(f"accuracy ({tag}): {counts['ok']}/{n} = {acc:.1%}   "
          f"| wrong={counts['wrong']} no_answer={counts['no_answer']} "
          f"timeout={counts['timeout']} error={counts['error']}")
    agree_str = f" | agreement {mean_agreement:.0%} mean" if mean_agreement is not None else ""
    print(f"cost: {metrics['mean_tokens']:.0f} tok/problem mean, {metrics['total_tokens']} "
          f"total (the per-token $ signal; flat-rate → read time){agree_str}")
    print(f"time: {time.time() - sweep_t0:.0f}s wall | {metrics['mean_time']:.1f}s/problem mean | "
          f"min {min(times):.1f}s / max {max(times):.1f}s" if times else "time: n/a")
    return metrics


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="samples",
                    help="samples | gsm8k | math500 | amc23 | aime24 | <path.jsonl>")
    ap.add_argument("--n", type=int, default=20, help="how many items for the HF benchmarks")
    ap.add_argument("--k", type=int, default=1, help="maj@k (k>1 votes k seeded chains)")
    ap.add_argument("--strategy", default="tir_fence",
                    choices=["tir_fence", "cot", "self_verify", "tools"],
                    help="the rung: tir_fence (specialist) | cot | self_verify | tools")
    ap.add_argument("--provider", default=None,
                    help="featherless | openrouter | novita | moonshot | deepinfra | selfhost")
    ap.add_argument("--model", default=None, help="model id (required for generalist strategies)")
    ap.add_argument("--max-tokens", type=int, default=None, help="per-round budget")
    ap.add_argument("--max-calls", type=int, default=None, help="max TIR rounds")
    ap.add_argument("--timeout", type=float, default=None, help="per-problem wall-clock seconds")
    args = ap.parse_args()

    if args.strategy in GENERALIST and not args.model:
        ap.error(f"--strategy {args.strategy} needs --model (the audition names the generalist)")
    # Thinking models share max_tokens between reasoning and the answer; a stingy cap returns
    # an empty \boxed{}. Give generalists a fat default budget unless overridden.
    if args.max_tokens is None and args.strategy in GENERALIST:
        args.max_tokens = 16384

    problems = load_data(args.data, args.n)
    print(f"audition: provider={args.provider or '(default)'} model={args.model or '(default)'} "
          f"strategy={args.strategy} data={args.data} n={len(problems)} k={args.k}")
    evaluate(problems, k=args.k, model=args.model, strategy=args.strategy,
             provider=args.provider, max_tokens=args.max_tokens, max_calls=args.max_calls,
             timeout=args.timeout)
