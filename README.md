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

Scaffold + handoff spec. **None of this has been run against live Modal/Lean infrastructure yet** — the `lean_image` build in particular is researched-but-untested. Read `PLAN.md` before executing; build the solver first (it has no Lean dependency), then the prover. `PLAN.md` carries the research findings with sources so you don't re-derive them.

## Quick start

```bash
uv sync

# solver (the spine — no Lean toolchain needed)
modal deploy serve.py                       # model server (Qwen2.5-Math by default)
uv run python solver_loop.py                # drive the TIR loop against a sample problem
modal deploy executor/modal_executor.py     # optional: remote executor for heavy compute

# prover (the extension — needs lean_image; see PLAN.md §4)
#   edit serve.py: swap MODEL to a prover, redeploy
modal deploy verifier/modal_verifier.py     # Lean verifier
uv run python prover_loop.py                # drive the compile-and-retry loop
```
