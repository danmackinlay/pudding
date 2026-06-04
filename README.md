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

**Pivoted to a workbench framing** (see `PLAN.md`): the product is a *rented generalist +
verification*, not the specialist TIR solver. The organizing idea is a **trust ladder** —
maj@k → self-verify → tool-check → Lean — and the repo is driven by an **audition**
(`eval.py --provider --strategy`) that picks the engine by results.

- **Built + tested** (Phase A, network-free): the provider registry (`providers.py`), the
  generalist `cot`/`self_verify` rungs (`strategies.py`), and the strategy dispatch in
  `solver_loop.py`, all on the shared streaming envelope — 24 tests pass.
- **Verified earlier** and reused as one contender: the `tir_fence` TIR loop end-to-end on
  Featherless, the local `Kernel`, the remote-executor swap, and the Open WebUI shim (`shim.py`).
- **Gated:** the Lean prover (Phase C) — research frozen in `PROVER_RESEARCH_ADDENDUM.md`.

> Why the pivot: Qwen2.5-Math-7B *and* 72B answer `7^999 mod 1000` as `43` (maj@8 unanimously
> wrong) despite the kernel returning `143` — a systematic "won't trust the tool" failure. The
> 2026 leaderboards are topped by rentable generalists instead. `PLAN.md` has the full reasoning.

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

# chat UI — run the loop behind an OpenAI-compatible shim, then point Open WebUI at it
direnv exec . uv run uvicorn shim:app --port 8000          # the TIR loop as /v1/chat/completions
WEBUI_AUTH=False OPENAI_API_BASE_URLS=http://localhost:8000/v1 OPENAI_API_KEYS=dummy \
  uvx --python 3.11 open-webui@latest serve --port 8080    # open http://localhost:8080 → tir-solver
#   details + Docker fallback: OPEN_WEBUI.md

# prover (stage 2 — see PROVER_RESEARCH_ADDENDUM.md; uses a prebuilt Kimina image)
```
