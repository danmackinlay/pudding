# FINDINGS — first audition (2026-06-04/05)

The raw boards from the first real audition, frozen here because `results/*.jsonl` is gitignored.
Conclusions are actioned in `WORKBENCH_PLAN.md`; this is the evidence.

## Provenance

- **Sets:** AIME (`AI-MO/aimo-validation-aime`) n=8, maj@4 · MATH-500 **level-5** (`HuggingFaceH4/MATH-500`) n=15, maj@2.
- **Engine:** OpenRouter generalists + one Featherless specialist (TIR). `audition.py`, async, concurrency 16 (specialist 3).
- **Caps:** generalists 16k tokens / per-problem 480–720s, *except* the **Kimi AIME rows = a long-cap
  re-run** (32k tokens, 1800s/problem, conc 4) to rule out clipped-clock artifacts — see finding #2.
- **Prices (OpenRouter, out $/M):** DeepSeek-V4-Pro 0.87 · Kimi-K2.6 3.42 · Qwen3.7-Max 3.75. `$ total` =
  completion-tokens × out-price (input ignored); Featherless is flat-rate. **Whole exploration ≈ $6.**
- `agree` = maj@k vote fraction (the confidence signal). `s/prob` = per-problem wall (chains run concurrently).

### AIME — n=8, maj@4

| contender | acc | agree | $ total | $/correct | tok/prob | s/prob | TTFT | tok/s |
|---|---|---|---|---|---|---|---|---|
| Qwen3.7-Max · CoT | 8/8 (100%) | 100% | $1.35 | $0.168 | 44883 | 209s | 1.1s | 77 |
| DeepSeek-V4-Pro · CoT | 6/8 (75%) | 100% | $0.14 | $0.023 | 19710 | 257s | 1.8s | 52 |
| Kimi-K2.6 · CoT | 5/8 (62%) | 100% | $0.66 | $0.131 | 24012 | 1071s | 1.0s | 95 |
| Kimi-K2.6 · self-verify | 3/8 (38%) | 100% | $0.65 | $0.217 | 23838 | 1373s | 1.1s | 85 |
| Qwen2.5-Math-72B · TIR | 0/8 (0%) | 0% | flat | flat | 0 | 428s | — | — |

### MATH-500 L5 — n=15, maj@2

| contender | acc | agree | $ total | $/correct | tok/prob | s/prob | TTFT | tok/s |
|---|---|---|---|---|---|---|---|---|
| Kimi-K2.6 · CoT | 14/15 (93%) | 93% | $0.28 | $0.020 | 5541 | 144s | 0.9s | 57 |
| Qwen3.7-Max · CoT | 14/15 (93%) | 93% | $0.27 | $0.019 | 4849 | 52s | 2.5s | 75 |
| DeepSeek-V4-Pro · CoT | 13/15 (87%) | 90% | $0.04 | $0.003 | 2919 | 50s | 1.8s | 60 |
| Kimi-K2.6 · self-verify | 11/15 (73%) | 96% | $0.39 | $0.036 | 7662 | 288s | 1.2s | 81 |
| Qwen2.5-Math-72B · TIR | 0/15 (0%) | 0% | flat | flat | 0 | 349s | — | — |

## The read

1. **Model texture.** Qwen3.7-Max = accuracy leader (AIME 8/8) but priciest ($0.17/correct).
   DeepSeek-V4-Pro = value champ ($0.003–0.02/correct, competitive accuracy). Kimi-K2.6 = fast
   decode but verbose/slow and **weaker on AIME** (5/8); strong on MATH-L5 (14/15).
2. **The Kimi AIME timeouts were masking real failures, not clipped successes.** The long-cap
   re-run (32k tokens, 30 min/problem) returned the *same* 5/8 cot / 3/8 self-verify — just slower
   (~18–23 min/problem). So Kimi genuinely can't crack those AIME problems; it isn't a config artifact.
3. **`self_verify` (rung 2) consistently *degrades* accuracy** — Kimi CoT→verify: 5/8→3/8 (AIME),
   93%→73% (MATH-L5). The single-pass critic over-corrects right answers. **maj@k agreement (100% on
   AIME wherever answered) is the reliable confidence signal**, not self-critique. → workbench leads
   with maj@k-deep, not self-verify (`WORKBENCH_PLAN.md` §2.5, §6).
4. **The specialist is dead weight** — Qwen2.5-Math TIR scored **0% on both** hard sets (timeouts +
   no parseable answers). The pivot to rented generalists is fully vindicated; drop it.
5. **AIME ≫ MATH-L5** in difficulty *and* token cost (~4× the tokens, e.g. Kimi 24k vs 5.5k tok/prob).
   "Level-5" competition math is far cheaper than olympiad.

## Caveats

- **Small samples** (n=8 / n=15) — directional texture, not leaderboard-grade numbers.
- **MATH-L5 was graded pre-`\dfrac`-fix** (commit `060b40e` landed during the sweep), so ≥1 `\dfrac`
  answer was mis-marked wrong — MATH-L5 scores are a slight *floor*. Re-run for exact figures.
- The self-verify finding is against the current `VERIFY_SYS` prompt; it may be fixable (see
  `WORKBENCH_PLAN.md` §6 — "only change on a definite error", or verify-then-vote).

## Reproduce / extend

```bash
# resumable + kill-safe; rows append to results/audition-<data>-k<k>.jsonl
PUDDING_HTTP_TIMEOUT=240 uv run python audition.py --data aime24 --n 8 --k 4 --concurrency 16 --verbose
PUDDING_HTTP_TIMEOUT=360 uv run python audition.py --data math500_hard --n 15 --k 2 --concurrency 16 --verbose
```
Lineup in `contenders.jsonl` (swap freely). Bigger n, more contenders, or the `tools` rung = next.
