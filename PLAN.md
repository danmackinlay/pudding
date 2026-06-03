# PLAN — maths reasoning on Modal: TIR solver, then a prover extension (handoff spec)

This is a self-contained handoff for a fresh agent or a human. It carries the design decisions, the research findings (with sources), the build recipe for the hard part, a testing checklist, and the open questions.

**Goal:** a working **TIR solver** pipeline (natural-language problem → code-executing reasoning → boxed answer) as the spine, then a **Lean prover** built as an *extension* of the same loop (formal statement → verified proof). This matches the arc of the blog post `notebook/automatic_maths.qmd` (in the `livingthing` repo): the worked example is the solver; `#prover` shows the prover is "the same shape."

## 0. Context & non-negotiables

- Companion to the post's worked example + `#prover` section. The post stays conceptual; the brittle plumbing lives here.
- **Decision (locked):** the **solver is the primary build**. Get the TIR loop working end-to-end first; the prover reuses its model-server and orchestrator skeleton with the executor swapped for a verifier. Do not build the prover before the solver runs.
- **Decision (locked):** three roles stay separable (model server / executor-or-verifier / orchestrator) so each can move between laptop and cloud. This is the whole pedagogical point — don't co-locate them "for simplicity".
- **Decision (locked):** the prover's core loop starts from a *formal* Lean 4 statement. Autoformalization is an optional front-end, not part of the core loop. Rationale in §6.
- I (the scaffolding agent) could **not** run Modal, a GPU, a Lean toolchain, or a Jupyter kernel. Every `.py` file is **untested**. Treat version numbers as starting guesses to verify, not as known-good.

## 1. Architecture (one skeleton, two fillings)

```
                        ┌──────────────────────────┐
   problem / statement ─► orchestrator (laptop)     │
                        │  - build prompt           │
                        │  - call model server   ───┼──► serve.py  (Modal GPU, vLLM, OpenAI API)
                        │  - extract fenced block    │      solver: Qwen2.5-Math / OpenMath-Nemotron
                        │  - call executor/verifier ─┼──► executor/  (solver: IPython kernel)
                        │  - splice result back      │      verifier/ (prover: Lean compiler)
                        │  - repeat until done       │
                        └──────────────────────────┘
```

| Role | Solver (spine) | Prover (extension) |
|---|---|---|
| **Model server** | `serve.py`, solver model | `serve.py`, model string swapped |
| **Executor / verifier** | `executor/` — IPython kernel, `run(code) -> str` | `verifier/` — Lean compiler, `run(proof) -> {ok, errors}` |
| **Orchestrator** | `solver_loop.py` — stop when no fresh code | `prover_loop.py` — stop when compiler accepts; feed errors back |

The only structural differences solver→prover: the executor becomes a verifier, the halting rule changes (no-more-code → compiler-accepts), and the token budget grows (proofs + CoT plans run long). Everything else is shared.

Fan-out (`fanout.py`): wrap one full chain as a Modal function and `.map` over seeds, each with its own executor/verifier — solver maj@k and prover Pass@k are the same pattern (post's `#fan-out`).

## 2. The solver (build this first)

### 2.1 Model + I/O
- Default **Qwen2.5-Math-7B-Instruct** for cheap bring-up; scale to **Qwen2.5-Math-72B-Instruct** or **OpenMath-Nemotron-32B** for real runs. Served under vLLM OpenAI-compatible.
- Qwen2.5-Math speaks markdown fences: it writes a ```` ```python ```` block and expects the result back inside a ```` ```output ```` block. Stop generation on the output-open fence, run the code, splice the result, continue. Final answer in `\boxed{}`.
- **Model-specific tags:** OpenMath-Nemotron wraps code in `<tool_call> … </tool_call>` instead — read the delimiters off the model's docs, not its name. `solver_loop.py` hard-codes Qwen's fences; parametrize if you swap.
- Test-time scaling for the headline number: `solve` k times at temperature ~0.6 and majority-vote the boxed answers (`fanout.py`).

### 2.2 Executor (the "sandbox")
- **Local default (`solver_loop.Kernel`):** a long-lived IPython kernel via `jupyter_client` — state persists across blocks (Qwen2.5-Math assumes a stateful session; a fresh subprocess per block breaks multi-step solutions). Security floor on a personal box = a wall-clock timeout; for anything shared, run inside Docker `--network none`.
- **Remote (`executor/modal_executor.py`):** when the computation is heavy (Gröbner basis, big symbolic integral, Sage/PARI, another model as a tool), move the executor off the laptop to a CPU-sized Modal container — sized to the *tool*, not the GPU model box. Same `run(code) -> str` interface; the loop body is untouched. NB the Modal call is `executor.run.remote(code)` (an adapter detail in `solver_loop.py`).

## 3. The prover (extension — only after §2 runs)

### 3.1 Lean REPL / verifier
- **`leanprover-community/repl`** — https://github.com/leanprover-community/repl — the canonical checker. JSON over stdin, commands separated by blank lines. Send `{"cmd": "<lean code>"}` (optionally `"env": N` to reuse an environment). Response JSON has `sorries` (each `{pos, goal, proofState}`), `messages` (each `{severity, pos, data}`), and `env` id. **A closed proof = empty `sorries` and no `error`-severity message.**
- **Kimina Lean Server** — https://github.com/project-numina/kimina-lean-server · paper https://arxiv.org/abs/2504.21230 — FastAPI **wrapper around that same REPL**. Adds a REPL pool (one per core) + an **import-header LRU cache** so `import Mathlib` isn't paid per proof (~1.9× speedup). REST: `POST /verify` with `{"codes": [{"custom_id": "...", "proof": "..."}], "infotree_type": "original"}`. Python client: `pip install kimina-client`, `client.check(snippets)`. **Use this for the verifier** (throughput + an official Dockerfile). Raw `repl` is the fallback primitive.
- Goedel-Prover-V2 ships its own scheduler over the REPL (`lean_compiler/repl_scheduler.py`) for reference.

### 3.2 Prover models (I/O contract)
- **DeepSeek-Prover-V2** (7B, 32K ctx; 671B) — https://github.com/deepseek-ai/DeepSeek-Prover-V2 · https://arxiv.org/abs/2504.21801. End-to-end at inference: give a complete Lean 4 statement ending `:= by sorry`; it emits a CoT proof plan then the full proof. Prompt template (apply via `apply_chat_template`, single user turn):
  ```
  Complete the following Lean 4 code:

  ```lean4
  {statement}
  ```

  Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof plan outlining the main proof steps and strategies.
  ```
  Statement carries `import Mathlib`, `set_option maxHeartbeats 0`, etc. 671B = 88.9% MiniF2F-test.
- **Goedel-Prover-V2** (8B, 32B) — https://github.com/Goedel-LM/Goedel-Prover-V2 · https://arxiv.org/abs/2508.03613. **Identical prompt template.** Distinguishing feature: **self-correction** — generate, feed Lean compiler errors back, revise (2 rounds); output grows 32K→40K tokens. 32B = 88.0% MiniF2F Pass@32, 90.4% in self-correction mode (beats DeepSeek-Prover-V2-671B). This self-correction loop IS our orchestrator loop.
- **Answer extraction:** take the last ```` ```lean4 … ``` ```` fence. Goedel's regex: `r'```lean4\n(.*?)\n```'` (DOTALL).

### 3.3 Serving
- All of the above serve under **vLLM OpenAI-compatible** (`vllm serve <model>`), same as the solver. Generation budget is the only real difference vs the solver: `max_tokens` 8192 (DeepSeek 7B) up to 32768 (Goedel 32B), ~40K for self-correction. Sampling: greedy for one attempt; `temperature≈0.6–0.9, top_p=0.95` for Pass@k diversity. No special stop tokens — extraction is by fence-parsing.

## 4. Building `lean_image` (the crunch — only relevant for the prover extension)

Toolchain via **`elan`**. Building Mathlib from source compiles 2500+ files and takes **hours** — do NOT do that. Instead:

1. Pin `lean-toolchain` to **exactly** the version Mathlib's CI cache was built against. **Mismatch → silent multi-hour full rebuild** (https://stackoverflow.com/questions/77280192). This is the single biggest trap (same class as a version-pinned pickle).
2. `lake exe cache get` pulls prebuilt `.olean` files from Mathlib CI (mathlib4 README) instead of compiling.
3. **Bake the built Mathlib into an image layer** so it isn't rebuilt per container.
4. Easiest path: start from the **Kimina Dockerfile** — `docker build --build-arg=LEAN_SERVER_LEAN_VERSION=v4.21.0 .`; its `setup.sh` "installs Lean, repl and mathlib4." Wrap that as a Modal image (`modal.Image.from_dockerfile` or `.from_registry`).
5. Runtime knobs: `LEAN_SERVER_MAX_REPL_MEM` (default 8G — per-REPL OOM is real), file-descriptor limits at high `max_workers`. See `verifier/lean_image_notes.md`.

**Unverified / to confirm during build:**
- On-disk size of the Mathlib `.olean` cache (no primary source found; commonly cited "a few GB"). Affects Volume sizing.
- Which Lean toolchain version the chosen prover model's proofs target (DeepSeek-Prover-V2 vs Goedel-Prover-V2 may assume different Mathlib snapshots — match the verifier to the model, or proofs fail to compile for spurious reasons).
- Whether to run the verifier as a Modal `@app.cls` (stateful, warm) or a `modal.Sandbox` per proof (isolated, the case-study choice). Start with `@app.cls`; move to Sandbox if a runaway proof wedges the server.

### 4.1 Mathlib-in-container precedent
- **Modal case study (crib from this):** https://modal.com/blog/building-an-rl-theorem-proving-workflow-on-modal · code https://github.com/agencyenterprise/modal-rl-theorem-case-study. Exactly our split: GPU vLLM generation, **kimina-lean-server image in Modal Sandboxes**, lightweight orchestrator, `.map()` fan-out, base weights in a Volume. NB: April-2026, authored by a Modal partner (AE Studio) — treat their cost/time figures as estimates.

## 5. Testing checklist (in order — solver first)

**Solver (the spine):**
1. [ ] `serve.py` deploys; an OpenAI client gets a completion from Qwen2.5-Math-7B (cheapest). Confirm it emits a ```` ```python ```` fence.
2. [ ] `solver_loop.py` end-to-end against a local `Kernel` on the sample (`7^999 mod 1000`): generate → run code → splice ```` ```output ```` → continue → `\boxed{}`. Confirm kernel state persists across blocks.
3. [ ] `executor/modal_executor.py` deploys; swap it in for the local `Kernel` (`executor.run.remote(code)`), loop body unchanged, same answer.
4. [ ] `fanout.py` maj@k: `.map` 32 chains, each its own executor, one shared endpoint; majority-vote the boxed answers.
5. [ ] Cost sanity: scale-to-zero idles the GPU between bursts.

**Prover (the extension — only once the solver works):**
6. [ ] `serve.py` with the prover model swap (DeepSeek-Prover-V2-7B) returns a ```` ```lean4 ```` fence.
7. [ ] `lean_image` builds and `lake exe cache get` hits the cache (build < ~10 min, not hours). Verify toolchain == Mathlib pin.
8. [ ] Kimina server answers `POST /verify` on a trivial good proof (`theorem t : 1 = 1 := by rfl`) → ok, and a bad one → errors.
9. [ ] `prover_loop.py` end-to-end on one easy MiniF2F statement: generate → extract → verify → (if errors) feed back → closed proof.
10. [ ] Self-correction (Goedel-32B): a first-attempt failure closes on round 2.
11. [ ] Pass@k fan-out: `.map` Pass@8, each with its own verifier; collect the first closing proof. Optionally point the model server at DeepSeek-Prover-V2-671B on DeepInfra instead of self-hosting.

## 6. Why autoformalization is out of the core loop

The solver consumes natural language; the prover consumes a *formal* statement. A "prove this English sentence" pipeline therefore needs an extra model in front (NL → Lean statement). That model is (a) a second moving part and (b) the **least reliable** stage (~55–75% faithful, §6.1), against a verifier that is exact. Bolting it into the core loop would muddy the symmetry with the solver. Standard prover benchmarks (MiniF2F, ProofNet) ship the formal statement, so the core loop starts there. A `formalize/` front-end can be a clearly-labelled optional add-on later.

### 6.1 Autoformalization models (for the optional front-end)
- **Kimina-Autoformalizer-7B** (https://huggingface.co/AI-MO/Kimina-Autoformalizer-7B), **Goedel-Formalizer-V2-8B/32B** (https://huggingface.co/Goedel-LM/Goedel-Formalizer-V2-8B). Both: NL statement → Lean 4 `theorem … := by sorry` (statement only). **Herald** (https://arxiv.org/abs/2410.10878) is the Mathlib4↔NL dataset/translator.
- Reliability: Goedel's eval on 300 Omni-MATH statements — Kimina-Formalizer 161/300, Goedel-Formalizer-V2-32B 228/300 → ~54–76% faithful. The weak link; needs round-trip or human check. Serve it the same vLLM way.

## 7. Open questions / future work

- **Frontend (separate research burden — see blog `#frontend`).** Solver UI: Open WebUI renders maths out of the box and has a built-in Python code interpreter (closest to turnkey for the TIR display). Gradio is the lightest programmable surface (config `latex_delimiters` for inline maths). Prover UI: no chat UI renders Lean proof state — embed `lean4web` (https://github.com/leanprover-community/lean4web, live at https://live.lean-lang.org) beside the chat. Mind the streaming-LaTeX flash (buffer until delimiters balance). Out of scope for the core loops; noted so it isn't forgotten.
- 671B prover off-the-shelf: wire the model-server role to DeepInfra/Novita instead of self-hosting, keep verifier on Modal. Cheapest entry point per the post.
- Premise selection / `lake exe cache` freshness as Mathlib moves.
- Whether the Kimina import-header cache helps or hurts when proofs use varied imports.

## 8. File map

- `serve.py` — shared **model server** (Modal GPU, vLLM). Defaults to the solver model; one-line swap to a prover model.
- `solver_loop.py` — **primary orchestrator**: TIR loop, local `Kernel` executor, pluggable to a remote one.
- `executor/modal_executor.py` — remote stateful IPython kernel (`run(code) -> str`) for heavy computation.
- `prover_loop.py` — **prover extension**: the same loop with compile-and-retry and a pluggable verifier.
- `verifier/modal_verifier.py` — `LeanVerifier` Modal class wrapping Kimina Lean Server; `run(proof) -> {ok, errors}`. The `lean_image` is the TODO.
- `verifier/lean_image_notes.md` — the §4 build recipe, expanded, with exact commands to try.
- `fanout.py` — `.map` fan-out: solver maj@k and prover Pass@k (same pattern).
