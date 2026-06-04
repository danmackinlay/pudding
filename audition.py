"""Matrix audition — sweep contenders over ONE problem set, print a ranked leaderboard.

Name your contenders once; get a sorted table (accuracy · agreement · TTFT · decode tok/s ·
tokens/problem · wall) plus a JSONL log under results/ that accumulates across runs. Within a
contender, problems/samples run concurrently (async, bounded by --concurrency); contenders run
sequentially so each provider's rate-limit behaviour is clean and the board prints incrementally.

    direnv exec . uv run python audition.py --data amc23 --n 10 --k 4
    direnv exec . uv run python audition.py --data aime24 --n 30 --k 8 --concurrency 8

A contender is {"provider","model","strategy","label"?}. Lineup from contenders.jsonl
('#' lines ignored), else the fallback below. A failing contender is caught and scored 0 so
one bad slug doesn't kill the sweep.
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

from eval import evaluate, load_data

GENERALIST = {"cot", "self_verify", "tools"}

DEFAULT_CONTENDERS = [
    {"provider": "featherless", "model": "Qwen/Qwen2.5-Math-72B-Instruct",
     "strategy": "tir_fence", "label": "Qwen2.5-Math-72B · TIR"},
    {"provider": "openrouter", "model": "moonshotai/kimi-k2.6",
     "strategy": "cot", "label": "Kimi-K2.6 · CoT"},
    {"provider": "openrouter", "model": "moonshotai/kimi-k2.6",
     "strategy": "self_verify", "label": "Kimi-K2.6 · self-verify"},
    {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro",
     "strategy": "cot", "label": "DeepSeek-V4-Pro · CoT"},
    {"provider": "openrouter", "model": "qwen/qwen3.7-max",
     "strategy": "cot", "label": "Qwen3.7-Max · CoT"},
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


async def run(args) -> None:
    contenders = load_contenders(args.contenders)
    problems = load_data(args.data, args.n)
    print(f"audition: {len(contenders)} contenders × {len(problems)} problems "
          f"({args.data}) · maj@{args.k} · concurrency {args.concurrency}\n")

    rows = []
    for c in contenders:
        label = c.get("label") or f'{c["model"]} [{c["strategy"]}]'
        mt = args.max_tokens or (16384 if c["strategy"] in GENERALIST else None)
        print(f"--- {label}  ({c['provider']} · {c['strategy']}) ---")
        try:
            m = await evaluate(problems, k=args.k, model=c["model"], strategy=c["strategy"],
                               provider=c["provider"], max_tokens=mt, timeout=args.timeout,
                               concurrency=args.concurrency, verbose=args.verbose)
        except Exception as e:  # noqa: BLE001 — one bad contender shouldn't kill the sweep
            print(f"  contender failed: {type(e).__name__}: {e}")
            m = {"acc": 0.0, "mean_tokens": 0.0, "mean_time": 0.0, "wall": 0.0,
                 "mean_agreement": None, "mean_ttft": None, "mean_decode": None,
                 "error": f"{type(e).__name__}: {e}"}
        rows.append({**c, "label": label, **m})
        print()

    # --- leaderboard ---
    rows.sort(key=lambda r: (r["acc"], -(r.get("mean_tokens") or 0)), reverse=True)
    print("=" * 96)
    print(f"{'contender':<34} {'acc':>6} {'agree':>6} {'ttft':>6} {'tok/s':>6} {'tok/prob':>9} {'wall':>6}")
    print("-" * 96)
    for r in rows:
        agree = r.get("mean_agreement")
        ttft = r.get("mean_ttft")
        dec = r.get("mean_decode")
        print(f"{r['label'][:34]:<34} {r['acc']:>6.1%} "
              f"{(f'{agree:.0%}' if agree is not None else '—'):>6} "
              f"{(f'{ttft:.1f}s' if ttft is not None else '—'):>6} "
              f"{(f'{dec:.0f}' if dec is not None else '—'):>6} "
              f"{(r.get('mean_tokens') or 0):>9.0f} {(r.get('wall') or 0):>5.0f}s")

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contenders", default="contenders.jsonl", help="JSONL lineup (see module doc)")
    ap.add_argument("--data", default="samples",
                    help="samples | gsm8k | math500 | amc23 | aime24 | <path.jsonl>")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--k", type=int, default=1, help="maj@k per contender")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=None, help="per-problem wall-clock seconds")
    ap.add_argument("--concurrency", type=int, default=6, help="max in-flight problems/samples")
    ap.add_argument("--verbose", action="store_true", help="per-item lines (default: summaries only)")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
