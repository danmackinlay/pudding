"""Evaluate the TIR solver over a problem set — accuracy, optionally maj@k.

Runs solver_loop.solve on each problem, pulls the last \\boxed{...}, and grades it.
The staircase (samples.jsonl) and the integer-answer benchmarks (GSM8K, AMC, AIME) grade
with a normalized `==`; MATH answers are LaTeX and need symbolic equality — there is a
commented `math_verify` hook for that (PLAN.md §9.2). Start on integer sets so a failure
points at the loop, not at the grader (PLAN.md §9.1).

    direnv exec . uv run python eval.py                       # 4-problem staircase, greedy
    direnv exec . uv run python eval.py --data gsm8k --n 20    # first 20 GSM8K
    direnv exec . uv run python eval.py --k 8                  # local maj@8 per problem
    direnv exec . uv run python eval.py --model Qwen/Qwen2.5-Math-72B-Instruct
"""
import argparse
import json
from collections import Counter

from solver_loop import solve, Kernel, extract_boxed


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


def load_math500(n: int = 20) -> list[dict]:
    """First n MATH-500 items. NB answers are LaTeX → needs the symbolic grader (§9.2)."""
    from datasets import load_dataset
    ds = load_dataset("HuggingFaceH4/MATH-500", split="test")
    return [{"problem": r["problem"], "answer": r["answer"]}
            for r in ds.select(range(min(n, len(ds))))]


# --- grading ---------------------------------------------------------------
def _as_number(s: str):
    try:
        return int(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        try:
            return float(s.replace(",", "").strip())
        except (ValueError, AttributeError):
            return None


def grade(pred: str | None, gold: str) -> bool:
    """Integer/decimal-aware equality (PLAN.md §9.2). Good for GSM8K/AMC/AIME/staircase.

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
def solve_graded(problem: str, k: int, model: str | None,
                 max_tokens: int | None = None, max_calls: int | None = None) -> str | None:
    """Greedy single shot (k==1) or local maj@k (k>1). Returns the (voted) boxed answer.

    Each chain gets its own Kernel (state isolation) and shuts it down so we don't leak
    kernels across a long eval. The Modal-distributed version is fanout.py. `max_tokens`/
    `max_calls` (None → solver_loop defaults) are the per-problem effort knobs.
    """
    kw = {}
    if max_tokens is not None:
        kw["max_tokens"] = max_tokens
    if max_calls is not None:
        kw["max_calls"] = max_calls

    if k <= 1:
        kern = Kernel()
        try:
            return extract_boxed(solve(problem, executor=kern, model=model, **kw))
        finally:
            kern.km.shutdown_kernel(now=True)

    answers = []
    for seed in range(k):
        kern = Kernel()
        try:
            ans = extract_boxed(solve(problem, executor=kern, temperature=0.6,
                                      seed=seed, model=model, **kw))
        finally:
            kern.km.shutdown_kernel(now=True)
        if ans:
            answers.append(ans)
    votes = Counter(answers)
    return votes.most_common(1)[0][0] if votes else None


def evaluate(problems: list[dict], k: int = 1, model: str | None = None,
             max_tokens: int | None = None, max_calls: int | None = None,
             timeout: float | None = None) -> float:
    """Grade `problems`, printing a per-item line and a status breakdown.

    A per-problem wall-clock `timeout` keeps the run tractable to debug: a single runaway
    (e.g. a non-empty repetition loop) reports `timeout` and the eval moves on instead of
    stalling. (The orphaned worker finishes in the background — a robust kill belongs to the
    Stage 1.6 effort/timeout work.) Status: ok | wrong | no_answer | timeout | error.
    """
    import concurrent.futures as cf
    import time

    counts = {"ok": 0, "wrong": 0, "no_answer": 0, "timeout": 0, "error": 0}
    times: list[float] = []
    sweep_t0 = time.time()
    for i, item in enumerate(problems, 1):
        pred, status, t0 = None, None, time.time()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(solve_graded, item["problem"], k, model, max_tokens, max_calls)
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
    # Token/$ accounting lands with solve_stream's usage (plan §1.6) — the metered plan is
    # flat-rate, so wall-time is the cost signal here.
    print(f"time: {wall:.0f}s wall | {mean:.1f}s/problem mean | "
          f"min {min(times):.1f}s / max {max(times):.1f}s" if times else "time: n/a")
    return acc


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="samples", help="samples | gsm8k | math500 | <path.jsonl>")
    ap.add_argument("--n", type=int, default=20, help="how many items for gsm8k/math500")
    ap.add_argument("--k", type=int, default=1, help="maj@k (k>1 votes k seeded chains)")
    ap.add_argument("--model", default=None, help="override SOLVER_MODEL")
    ap.add_argument("--max-tokens", type=int, default=None, help="per-round budget (default: solver_loop)")
    ap.add_argument("--max-calls", type=int, default=None, help="max TIR rounds (default: solver_loop)")
    ap.add_argument("--timeout", type=float, default=None, help="per-problem wall-clock seconds")
    args = ap.parse_args()

    if args.data == "samples":
        problems = load_samples()
    elif args.data == "gsm8k":
        problems = load_gsm8k(args.n)
    elif args.data == "math500":
        problems = load_math500(args.n)
    else:
        problems = load_samples(args.data)

    evaluate(problems, k=args.k, model=args.model)
