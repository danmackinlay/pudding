"""Matrix audition — sweep contenders over ONE problem set, print a ranked leaderboard.

This is the "make it easy to audition" tool: instead of running eval.py N times and collating
accuracy by eye, name your contenders once and get a sorted table (accuracy · agreement ·
tokens/problem · seconds/problem), plus an appended JSONL log so comparisons accumulate.

    direnv exec . uv run python audition.py                          # contenders.jsonl, staircase, k=1
    direnv exec . uv run python audition.py --data amc23 --n 10 --k 4
    direnv exec . uv run python audition.py --data aime24 --n 30 --k 8 --verbose

A contender is {"provider", "model", "strategy", "label"?}. Lineup is read from contenders.jsonl
(EDIT the model slugs to your OpenRouter catalogue picks); '#'-prefixed lines are ignored. A
failing contender (bad slug, missing key) is caught and scored 0 so the rest of the sweep runs.
"""
import argparse
import json
import time
from pathlib import Path

from eval import load_data, evaluate

GENERALIST = {"cot", "self_verify", "tools"}

# Fallback lineup if contenders.jsonl is absent. EDIT slugs to taste (these are starting points;
# kimi-k2.6 is a confirmed OpenRouter slug, the specialist one is on Featherless).
DEFAULT_CONTENDERS = [
    {"provider": "featherless", "model": "nvidia/OpenMath-Nemotron-32B",
     "strategy": "tir_fence", "label": "OpenMath-Nemotron-32B · TIR"},
    {"provider": "openrouter", "model": "moonshotai/kimi-k2.6",
     "strategy": "cot", "label": "Kimi-K2.6 · CoT"},
    {"provider": "openrouter", "model": "moonshotai/kimi-k2.6",
     "strategy": "self_verify", "label": "Kimi-K2.6 · self-verify"},
]


def load_contenders(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return DEFAULT_CONTENDERS
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(json.loads(line))
    return out or DEFAULT_CONTENDERS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contenders", default="contenders.jsonl", help="JSONL lineup (see module doc)")
    ap.add_argument("--data", default="samples",
                    help="samples | gsm8k | math500 | amc23 | aime24 | <path.jsonl>")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--k", type=int, default=1, help="maj@k per contender")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None, help="per-problem wall-clock seconds")
    ap.add_argument("--verbose", action="store_true", help="show per-item lines (default: summaries only)")
    args = ap.parse_args()

    contenders = load_contenders(args.contenders)
    problems = load_data(args.data, args.n)
    print(f"audition: {len(contenders)} contenders × {len(problems)} problems "
          f"({args.data}) · maj@{args.k}\n")

    rows = []
    for c in contenders:
        label = c.get("label") or f'{c["model"]} [{c["strategy"]}]'
        # thinking models need a fat budget; specialists keep the loop default
        mt = args.max_tokens or (16384 if c["strategy"] in GENERALIST else None)
        print(f"--- {label}  ({c['provider']} · {c['strategy']}) ---")
        try:
            m = evaluate(problems, k=args.k, model=c["model"], strategy=c["strategy"],
                         provider=c["provider"], max_tokens=mt, timeout=args.timeout,
                         verbose=args.verbose)
        except Exception as e:  # noqa: BLE001 — one bad contender shouldn't kill the sweep
            print(f"  contender failed: {type(e).__name__}: {e}")
            m = {"acc": 0.0, "mean_tokens": 0.0, "mean_time": 0.0,
                 "mean_agreement": None, "error": f"{type(e).__name__}: {e}"}
        rows.append({**c, "label": label, **m})
        print()

    # --- leaderboard ---
    rows.sort(key=lambda r: (r["acc"], -(r.get("mean_tokens") or 0)), reverse=True)
    print("=" * 84)
    print(f"{'contender':<36} {'acc':>6} {'agree':>6} {'tok/prob':>9} {'s/prob':>7}")
    print("-" * 84)
    for r in rows:
        agree = r.get("mean_agreement")
        agree_s = f"{agree:.0%}" if agree is not None else "  —"
        print(f"{r['label'][:36]:<36} {r['acc']:>6.1%} {agree_s:>6} "
              f"{(r.get('mean_tokens') or 0):>9.0f} {(r.get('mean_time') or 0):>7.1f}")

    # --- persist (accumulates across runs) ---
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)
    fp = out_dir / f"audition-{args.data}-k{args.k}.jsonl"
    stamp = int(time.time())
    with fp.open("a") as f:
        for r in rows:
            rec = {k: v for k, v in r.items() if k != "counts"}
            rec["ts"], rec["data"], rec["k"], rec["n"] = stamp, args.data, args.k, len(problems)
            f.write(json.dumps(rec) + "\n")
    print(f"\nwrote {fp}")


if __name__ == "__main__":
    main()
