# pudding

> The proof of the pudding is in the eating.

Worked code for running **mathematical-reasoning LLMs on Modal**: a GPU-served model, a tool that runs the model's work, and a local loop that drives them. The spine is a **TIR solver** (natural-language problem → code-executing reasoning → boxed answer); a **Lean prover** is built on top as an extension (formal statement → verified proof). Companion to the worked example and `#prover` section of [*Maths and proof models, applied*](https://danmackinlay.name/notebook/automatic_maths.html) — the post sketches the architecture, this repo is the version-sensitive plumbing that does not belong in prose.

## The shape

One skeleton, two fillings. Three separable roles; the solver and the prover differ only in the middle one:

| Role | Solver (the spine) | Prover (the extension) | Runs on |
|---|---|---|---|
| **Model server** | `serve.py` — vLLM serving Qwen2.5-Math | same file, model string swapped to DeepSeek-/Goedel-Prover-V2 | Modal, GPU, scale-to-zero |
| **Executor / verifier** | `executor/` — IPython kernel, `run(code) -> str` | `verifier/` — Kimina Lean Server, `run(proof) -> {ok, errors}` | Modal, CPU (local kernel by default) |
| **Orchestrator** | `solver_loop.py` — stop when no fresh code | `prover_loop.py` — stop when the compiler accepts; feed errors back | laptop (or a Modal function for fan-out) |

The prover is the solver with three changes: the executor becomes a Lean verifier, the halting rule flips (no-more-code → compiler-accepts), and the token budget grows. **The only genuinely new piece is the verifier image** — see `verifier/lean_image_notes.md`.

## Scope

- **In:** the TIR solver loop, end to end; then a "formal Lean statement → verified proof" loop starting from `theorem … := by sorry`.
- **Out (deliberately):** autoformalization (natural-language → Lean statement). That is a separate, ~55–75%-reliable model; see `PLAN.md` §6. A stub front-end may land later under `formalize/`.

## Status

**The solver spine is verified end-to-end** against a live metered endpoint (Featherless)
and a Modal-hosted executor: the TIR loop, the local IPython `Kernel` (state persists,
errors captured), the remote-executor swap, and `eval.py` over the §9.4 staircase all run
(3/4 — the one miss is a model trap, see below). The **prover is stage 2** and untested;
its research is frozen in `PROVER_RESEARCH_ADDENDUM.md` (which corrects two guesses in
`PLAN.md` — there's a prebuilt Kimina image, so no multi-hour `lean_image` build). Read
`PLAN.md` before executing.

> Known model quirk (not a pipeline bug): Qwen2.5-Math-7B *and* 72B answer `7^999 mod 1000`
> as `43` even though the kernel correctly returns `143` — and maj@8 is unanimously wrong,
> so it's a systematic "won't trust the tool" error. The other staircase problems pass.

## Setup

Secrets live in `.env` (git-ignored); `.envrc` (`dotenv`) makes **direnv** load them — get
a Featherless key into `.env` as `FEATHERLESS_API_KEY=…`, then `direnv allow`. Run scripts
as `direnv exec . uv run …` (your own shell can drop the prefix; direnv auto-loads on `cd`).

## Quick start

```bash
uv sync
direnv allow

# solver (the spine) — metered tokens from Featherless, executor stays local
direnv exec . uv run python solver_loop.py        # TIR loop on a sample problem
direnv exec . uv run python eval.py               # grade the 4-problem staircase
direnv exec . uv run python eval.py --data gsm8k --n 20   # or a benchmark slice

# scale out (optional)
direnv exec . uv run modal deploy executor/modal_executor.py  # remote executor (heavy compute)
direnv exec . uv run modal run fanout.py --k 8                # maj@k over Modal .map
#   self-host the model instead of metering:  modal deploy serve.py  +  set SOLVER_BASE_URL

# prover (stage 2 — see PROVER_RESEARCH_ADDENDUM.md; uses a prebuilt Kimina image)
```
