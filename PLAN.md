# PLAN — Lean prover on Modal (handoff spec)

This is a self-contained handoff for a fresh agent or a human. It carries the design decisions, the research findings (with sources), the build recipe for the hard part, a testing checklist, and the open questions. **Goal:** a working "formal Lean statement → verified proof" pipeline matching the architecture in the blog post `notebook/automatic_maths.qmd` (in the `livingthing` repo).

## 0. Context & non-negotiables

- Companion to the blog post's `#prover` section. The post must stay conceptual; the brittle plumbing lives here.
- **Decision (locked):** the core loop starts from a *formal* Lean 4 statement. Autoformalization is an optional front-end, not part of the core loop. Rationale in §4.
- **Decision (locked):** three roles stay separable (model server / verifier / orchestrator) so each can move between laptop and cloud. This is the whole pedagogical point — don't co-locate them "for simplicity".
- I (the scaffolding agent) could **not** run Modal, a GPU, or a Lean toolchain. Everything in `serve.py`, `prover_loop.py`, `verifier/` is **untested**. Treat version numbers as starting guesses to verify, not as known-good.

## 1. Architecture

```
                         ┌─────────────────────────┐
   formal statement ───► │ orchestrator (laptop)    │
   theorem … := by sorry │  prover_loop.py          │
                         │  - build prompt          │
                         │  - call model server  ───┼──► serve.py  (Modal GPU, vLLM, OpenAI API)
                         │  - extract ```lean4 fence │       DeepSeek-Prover-V2 / Goedel-Prover-V2
                         │  - call verifier       ───┼──► modal_verifier.py (Modal CPU)
                         │  - if errors, feed back   │       Kimina Lean Server + Mathlib image
                         │    and retry (≤N rounds)  │
                         └─────────────────────────┘
```

Fan-out (Pass@k): wrap `prove_one(statement, seed)` as a Modal function and `.map` over seeds, each with its own verifier Sandbox — same pattern as the solver's maj@k in the post's `#fan-out`.

## 2. Research findings (verified against primary sources — don't re-derive)

### 2.1 Lean REPL / verifier
- **`leanprover-community/repl`** — https://github.com/leanprover-community/repl — the canonical checker. JSON over stdin, commands separated by blank lines. Send `{"cmd": "<lean code>"}` (optionally `"env": N` to reuse an environment). Response JSON has `sorries` (each `{pos, goal, proofState}`), `messages` (each `{severity, pos, data}` — errors/warnings), and `env` id. **A closed proof = empty `sorries` and no `error`-severity message.**
- **Kimina Lean Server** — https://github.com/project-numina/kimina-lean-server · paper https://arxiv.org/abs/2504.21230 — FastAPI **wrapper around that same REPL**. Adds a REPL pool (one per core) + an **import-header LRU cache** so `import Mathlib` isn't paid per proof (~1.9× speedup). REST: `POST /verify` with `{"codes": [{"custom_id": "...", "proof": "..."}], "infotree_type": "original"}`. Python client: `pip install kimina-client`, `client.check(snippets)`. **Use this for the verifier** (throughput + an official Dockerfile). Raw `repl` is the fallback primitive.
- Goedel-Prover-V2 ships its own scheduler over the REPL (`lean_compiler/repl_scheduler.py`) if you want a reference impl.

### 2.2 Prover models (I/O contract)
- **DeepSeek-Prover-V2** (7B, 32K ctx; 671B) — https://github.com/deepseek-ai/DeepSeek-Prover-V2 · https://arxiv.org/abs/2504.21801. End-to-end at inference: give a complete Lean 4 statement ending `:= by sorry`; it emits a CoT proof plan then the full proof. Prompt template (apply via `apply_chat_template`, single user turn):
  ```
  Complete the following Lean 4 code:

  ```lean4
  {statement}
  ```

  Before producing the Lean 4 code to formally prove the given theorem, provide a detailed proof plan outlining the main proof steps and strategies.
  ```
  Statement carries `import Mathlib`, `set_option maxHeartbeats 0`, etc. 671B = 88.9% MiniF2F-test.
- **Goedel-Prover-V2** (8B, 32B) — https://github.com/Goedel-LM/Goedel-Prover-V2 · https://arxiv.org/abs/2508.03613. **Identical prompt template** to DeepSeek-Prover-V2. Distinguishing feature: **self-correction** — generate, feed Lean compiler errors back, revise (2 rounds); output grows 32K→40K tokens. 32B = 88.0% MiniF2F Pass@32, 90.4% in self-correction mode (beats DeepSeek-Prover-V2-671B). This self-correction loop IS our orchestrator loop.
- **Answer extraction:** take the last ```` ```lean4 … ``` ```` fence. Goedel's regex: `r'```lean4\n(.*?)\n```'` (DOTALL).

### 2.3 Serving
- All of the above serve under **vLLM OpenAI-compatible** (`vllm serve <model>`), same as a solver. Generation budget is the only real difference: `max_tokens` 8192 (DeepSeek 7B) up to 32768 (Goedel 32B), ~40K for self-correction. Sampling: greedy for a single attempt; `temperature≈0.6–0.9, top_p=0.95` for Pass@k diversity. No special stop tokens — extraction is by fence-parsing.

### 2.4 Autoformalization (OUT of core loop — see §4)
- Models: **Kimina-Autoformalizer-7B** (https://huggingface.co/AI-MO/Kimina-Autoformalizer-7B), **Goedel-Formalizer-V2-8B/32B** (https://huggingface.co/Goedel-LM/Goedel-Formalizer-V2-8B). Both: NL statement → Lean 4 `theorem … := by sorry` (statement only). **Herald** (https://arxiv.org/abs/2410.10878) is the Mathlib4↔NL dataset/translator.
- Reliability: Goedel's eval on 300 Omni-MATH statements — Kimina-Formalizer 161/300, Goedel-Formalizer-V2-32B 228/300 → ~54–76% faithful. This is the weak link; the verifier is exact, the formalizer is not. Needs round-trip or human check.

### 2.5 Mathlib-in-container precedent
- **Modal case study (crib from this):** https://modal.com/blog/building-an-rl-theorem-proving-workflow-on-modal · code https://github.com/agencyenterprise/modal-rl-theorem-case-study. Exactly our split: GPU vLLM generation, **kimina-lean-server image in Modal Sandboxes** (a bad proof can hang/crash the checker → isolate it), lightweight orchestrator, `.map()` fan-out, base weights in a Volume. NB: April-2026, authored by a Modal partner (AE Studio) — treat their cost/time figures as estimates.

## 3. Building `lean_image` (the crunch — this is why the repo exists)

Toolchain via **`elan`**. Building Mathlib from source compiles 2500+ files and takes **hours** — do NOT do that. Instead:

1. Pin `lean-toolchain` to **exactly** the version Mathlib's CI cache was built against. **Mismatch → silent multi-hour full rebuild** (https://stackoverflow.com/questions/77280192). This is the single biggest trap (same class as a version-pinned pickle).
2. `lake exe cache get` pulls prebuilt `.olean` files from Mathlib CI (mathlib4 README) instead of compiling.
3. **Bake the built Mathlib into an image layer** so it isn't rebuilt per container.
4. Easiest path: start from the **Kimina Dockerfile** — `docker build --build-arg=LEAN_SERVER_LEAN_VERSION=v4.21.0 .`; its `setup.sh` "installs Lean, repl and mathlib4." Wrap that as a Modal image (`modal.Image.from_dockerfile` or `.from_registry`).
5. Runtime knobs: `LEAN_SERVER_MAX_REPL_MEM` (default 8G — per-REPL OOM is real), file-descriptor limits at high `max_workers`.

**Unverified / to confirm during build:**
- On-disk size of the Mathlib `.olean` cache (no primary source found; commonly cited "a few GB"). Affects Volume sizing.
- Which Lean toolchain version the chosen prover model's proofs target (DeepSeek-Prover-V2 vs Goedel-Prover-V2 may assume different Mathlib snapshots — match the verifier to the model, or proofs will fail to compile for spurious reasons).
- Whether to run the verifier as a Modal `@app.cls` (stateful, warm) or a `modal.Sandbox` per proof (isolated, the case-study choice). Start with `@app.cls`; move to Sandbox if a runaway proof wedges the server.

## 4. Why autoformalization is out of the core loop

A prover consumes a *formal* statement; the solver consumes natural language. A "prove this English sentence" pipeline therefore needs an extra model in front (NL → Lean statement). That model is (a) a second moving part and (b) the **least reliable** stage (§2.4, ~55–75%), against a verifier that is exact. Bolting it into the core loop would turn one clean example into a three-model chain and muddy the symmetry with the solver. Standard prover benchmarks (MiniF2F, ProofNet) ship the formal statement, so the core loop starts there. A `formalize/` front-end (Kimina-Autoformalizer-7B served the same vLLM way, with round-trip checking) can be a clearly-labelled optional add-on later.

## 5. Testing checklist (in order)

1. [ ] `serve.py` deploys; `curl`/OpenAI client gets a completion from DeepSeek-Prover-V2-7B (cheapest). Confirm prompt template + chat formatting produce a ```lean4 fence.
2. [ ] `lean_image` builds and `lake exe cache get` actually hit the cache (build < ~10 min, not hours). Verify toolchain == Mathlib pin.
3. [ ] Kimina server in the image answers `POST /verify` on a trivial known-good proof (`theorem t : 1 = 1 := by rfl`) → `ok`, and a known-bad one → errors.
4. [ ] `prover_loop.py` end-to-end on one easy MiniF2F statement: generate → extract → verify → (if needed) feed errors back → closed proof.
5. [ ] Self-correction: confirm a first-attempt failure with errors fed back can close on round 2 (Goedel-32B).
6. [ ] Fan-out: `.map` Pass@8 on one statement, each with its own verifier; collect first closing proof.
7. [ ] Cost sanity: scale-to-zero actually idles the GPU; a metered alternative (DeepSeek-Prover-V2-671B on DeepInfra) for the model server, verifier still local/Modal.

## 6. Open questions / future work

- **Frontend (separate research burden — see blog `#frontend`).** No chat UI renders Lean proof state; embed `lean4web` (https://github.com/leanprover-community/lean4web, live at https://live.lean-lang.org) beside a chat frontend. Out of scope for the core loop; note it here so it isn't forgotten.
- 671B prover off-the-shelf: wire the model-server role to DeepInfra/Novita instead of self-hosting, keep verifier on Modal. Cheapest entry point per the post.
- Premise selection / `lake exe cache` freshness as Mathlib moves.
- Whether the Kimina import-header cache helps or hurts when proofs use varied imports.

## 7. File map

- `serve.py` — prover model server (Modal GPU, vLLM). Model swap of the solver's `serve_qwen_math.py`.
- `prover_loop.py` — orchestrator: prompt build, fence extraction, compile-and-retry, pluggable verifier client.
- `verifier/modal_verifier.py` — `LeanVerifier` Modal class wrapping Kimina Lean Server; `run(proof) -> {ok, errors}`. The `lean_image` is the TODO.
- `verifier/lean_image_notes.md` — the §3 build recipe, expanded, with the exact commands to try.
