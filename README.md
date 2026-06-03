# pudding

> The proof of the pudding is in the eating.

A worked **Lean 4 prover pipeline on Modal**: a GPU-served prover model, a CPU-served Lean verifier, and a local compile-and-retry orchestrator. This is the companion code for the "prover is the same shape" section of [*Maths and proof models, applied*](https://danmackinlay.name/notebook/automatic_maths.html) — the blog post sketches the architecture; this repo is the version-sensitive plumbing that does not belong in prose.

## The shape

Three separable roles (same decomposition as the TIR solver in the post):

| Role | Here | Runs on |
|---|---|---|
| **Model server** | `serve.py` — vLLM serving DeepSeek-Prover-V2 / Goedel-Prover-V2, OpenAI-compatible | Modal, GPU (H100), scale-to-zero |
| **Verifier** (the "sandbox") | `verifier/modal_verifier.py` — Kimina Lean Server in a Mathlib image, `run(proof) -> {ok, errors}` | Modal, CPU, isolated Sandbox |
| **Orchestrator** | `prover_loop.py` — prompt → generate → extract → verify → feed errors back | laptop (or a Modal function for fan-out) |

The model server is a one-line model swap of the solver's `serve_qwen_math.py`. The orchestrator is the solver's loop with a different halting rule (compiler-accepts, not no-more-code). **The only new piece is the verifier image** — see `verifier/lean_image_notes.md`.

## Scope

- **In:** a "formal Lean statement in → verified proof out" loop. Start from `theorem … := by sorry`.
- **Out (deliberately):** autoformalization (natural-language → Lean statement). That is a separate, ~55–75%-reliable model; see `PLAN.md` §Autoformalization. A stub front-end may land later under `formalize/`.

## Status

Scaffold + handoff spec. **None of this has been run against live Modal/Lean infrastructure yet** — the `lean_image` build in particular is researched-but-untested. Read `PLAN.md` before executing; it carries the research findings (with sources) so you don't have to re-derive them.

## Quick start (once `lean_image` is built — see PLAN.md)

```bash
uv sync
modal deploy serve.py                    # prover model server
modal deploy verifier/modal_verifier.py  # Lean verifier
uv run python prover_loop.py             # drive the loop against a sample theorem
```
