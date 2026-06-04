"""The audition runner — grade (model × strategy × provider) over a problem set.

Runs the chosen strategy on each problem, pulls the last \\boxed{...}, and grades it. This is
how we pick the engine and kill dead rungs by results (PLAN.md §3): hold the problem set +
grader constant, vary the contender.

    # specialist TIR (the incumbent), metered tokens, local kernel
    eval.py --provider featherless --model OpenMath-Nemotron-32B --strategy tir_fence --data aime24 --k 8
    # generalist, pure chain-of-thought (rung 1), no executor
    eval.py --provider moonshot   --model kimi-k2.6              --strategy cot       --data aime24 --k 8
    # generalist, self-verification (rung 2)
    eval.py --provider deepinfra  --model Qwen/Qwen3-235B-A22B-Thinking --strategy self_verify --data amc23 --k 4

Integer-answer sets (staircase, GSM8K, AMC, AIME) grade with a normalized `==`. MATH-500 is
LaTeX → needs symbolic equality; the `math_verify` hook below is the swap point (PLAN.md §6.3).
Start on integer sets so a failure points at the loop, not the grader.
"""
import argparse
import json
from collections import Counter

from solver_loop import solve, Kernel, extract_boxed

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
    """First n MATH-500 items. NB answers are LaTeX → needs the symbolic grader (§6.3)."""
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
    """Integer/decimal-aware equality (PLAN.md §6.3). Good for GSM8K/AMC/AIME/staircase.

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


# --- run -------------------------------------------------------------------
def solve_graded(problem: str, k: int, model: str | None, *, strategy: str = "tir_fence",
                 provider: str | None = None, max_tokens: int | None = None,
                 max_calls: int | None = None) -> str | None:
    """Greedy single shot (k==1) or local maj@k (k>1). Returns the (voted) boxed answer.

    Only executor-driven strategies (tir_fence/tools) get a Kernel; cot/self_verify are pure
    chat calls. Each chain's kernel is shut down so a long eval doesn't leak kernels.
    """
    kw = {"strategy": strategy, "provider": provider}
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if max_calls is not None:
        kw["max_calls"] = max_calls
    needs = strategy in NEEDS_EXECUTOR

    def one(seed=None, temperature=0.0) -> str | None:
        kern = Kernel() if needs else None
        try:
            return extract_boxed(solve(problem, executor=kern, model=model,
                                       temperature=temperature, seed=seed, **kw))
        finally:
            if kern is not None:
                kern.km.shutdown_kernel(now=True)

    if k <= 1:
        return one()
    answers = [a for s in range(k) if (a := one(seed=s, temperature=0.6))]
    return Counter(answers).most_common(1)[0][0] if answers else None


def evaluate(problems: list[dict], k: int = 1, model: str | None = None, *,
             strategy: str = "tir_fence", provider: str | None = None,
             max_tokens: int | None = None, max_calls: int | None = None,
             timeout: float | None = None) -> float:
    """Grade `problems`, printing a per-item line and a status breakdown.

    A per-problem wall-clock `timeout` keeps the run tractable: a single runaway reports
    `timeout` and the eval moves on. Status: ok | wrong | no_answer | timeout | error.
    """
    import concurrent.futures as cf
    import time

    counts = {"ok": 0, "wrong": 0, "no_answer": 0, "timeout": 0, "error": 0}
    times: list[float] = []
    sweep_t0 = time.time()
    for i, item in enumerate(problems, 1):
        pred, status, t0 = None, None, time.time()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(solve_graded, item["problem"], k, model, strategy=strategy,
                            provider=provider, max_tokens=max_tokens, max_calls=max_calls)
            try:
                pred = fut.result(timeout=timeout)
            except cf.TimeoutError:
                status = "timeout"
            except Exception as e:  # noqa: BLE001 — surface, don't crash the sweep
                status, pred = "error", f"{type(e).__name__}: {e}"[:40]
        elapsed = time.time() - t0
        times.append(elapsed)
        if status is None:
            if grade(pred, item["answer"]):
                status = "ok"
            elif pred in (None, ""):
                status = "no_answer"
            else:
                status = "wrong"
        counts[status] += 1
        mark = {"ok": "✓", "wrong": "✗", "no_answer": "∅", "timeout": "⏱", "error": "!"}[status]
        prob = item["problem"][:50] + ("…" if len(item["problem"]) > 50 else "")
        print(f"{mark} [{i:>3}/{len(problems)}] {status:<9} {elapsed:5.1f}s "
              f"pred={str(pred):<10} gold={item['answer']:<8} {prob}", flush=True)

    n = len(problems)
    acc = counts["ok"] / n if n else 0.0
    tag = f"maj@{k}" if k > 1 else "greedy"
    wall = time.time() - sweep_t0
    mean = sum(times) / len(times) if times else 0.0
    print("=" * 72)
    print(f"accuracy ({tag}): {counts['ok']}/{n} = {acc:.1%}   "
          f"| wrong={counts['wrong']} no_answer={counts['no_answer']} "
          f"timeout={counts['timeout']} error={counts['error']}")
    # Time as a first-class diagnostic (generation-bound vs loop-bound is invisible otherwise).
    # NEXT (PLAN.md §3): per-token cost (completion_tokens) + maj@k agreement margin as the
    # confidence signal — both need `solve` to surface usage alongside the boxed answer.
    print(f"time: {wall:.0f}s wall | {mean:.1f}s/problem mean | "
          f"min {min(times):.1f}s / max {max(times):.1f}s" if times else "time: n/a")
    return acc


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
                    help="featherless | novita | moonshot | openrouter | deepinfra | selfhost")
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
             provider=args.provider, max_tokens=args.max_tokens, max_calls=args.max_calls)
