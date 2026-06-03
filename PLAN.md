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
2. [ ] `solver_loop.py` end-to-end against a local `Kernel` on the sample (`7^999 mod 1000`): generate → run code → splice ```` ```output ```` → continue → `\boxed{}`. Confirm kernel state persists across blocks. Then run the four-problem staircase, then a handful of GSM8K items — **§9** has the datasets and the grading caveat.
3. [ ] `executor/modal_executor.py` deploys; swap it in for the local `Kernel` (`executor.run.remote(code)`), loop body unchanged, same answer.
4. [ ] `fanout.py` maj@k: `.map` 32 chains, each its own executor, one shared endpoint; majority-vote the boxed answers.
5. [ ] Cost sanity: scale-to-zero idles the GPU between bursts.

**Prover (the extension — only once the solver works):**
6. [ ] `serve.py` with the prover model swap (DeepSeek-Prover-V2-7B) returns a ```` ```lean4 ```` fence.
7. [ ] `lean_image` builds and `lake exe cache get` hits the cache (build < ~10 min, not hours). Verify toolchain == Mathlib pin.
8. [ ] Kimina server answers `POST /verify` on a trivial good proof (`theorem t : 1 = 1 := by rfl`) → ok, and a bad one → errors.
9. [ ] `prover_loop.py` end-to-end on one easy MiniF2F statement: generate → extract → verify → (if errors) feed back → closed proof.
10. [ ] Self-correction (Goedel-32B): a first-attempt failure closes on round 2.
11. [ ] Pass@k fan-out: `.map` Pass@8, each with its own verifier; collect the first closing proof. Optionally point the model server at DeepSeek-Prover-V2-671B on Novita instead of self-hosting.

## 6. Why autoformalization is out of the core loop

The solver consumes natural language; the prover consumes a *formal* statement. A "prove this English sentence" pipeline therefore needs an extra model in front (NL → Lean statement). That model is (a) a second moving part and (b) the **least reliable** stage (~55–75% faithful, §6.1), against a verifier that is exact. Bolting it into the core loop would muddy the symmetry with the solver. Standard prover benchmarks (MiniF2F, ProofNet) ship the formal statement, so the core loop starts there. A `formalize/` front-end can be a clearly-labelled optional add-on later.

### 6.1 Autoformalization models (for the optional front-end)
- **Kimina-Autoformalizer-7B** (https://huggingface.co/AI-MO/Kimina-Autoformalizer-7B), **Goedel-Formalizer-V2-8B/32B** (https://huggingface.co/Goedel-LM/Goedel-Formalizer-V2-8B). Both: NL statement → Lean 4 `theorem … := by sorry` (statement only). **Herald** (https://arxiv.org/abs/2410.10878) is the Mathlib4↔NL dataset/translator.
- Reliability: Goedel's eval on 300 Omni-MATH statements — Kimina-Formalizer 161/300, Goedel-Formalizer-V2-32B 228/300 → ~54–76% faithful. The weak link; needs round-trip or human check. Serve it the same vLLM way.

## 7. Open questions / future work

- **Frontend (separate research burden — see blog `#frontend`).** Solver UI: Open WebUI renders maths out of the box and has a built-in Python code interpreter (closest to turnkey for the TIR display). Gradio is the lightest programmable surface (config `latex_delimiters` for inline maths). Prover UI: no chat UI renders Lean proof state — embed `lean4web` (https://github.com/leanprover-community/lean4web, live at https://live.lean-lang.org) beside the chat. Mind the streaming-LaTeX flash (buffer until delimiters balance). Out of scope for the core loops; noted so it isn't forgotten.
- 671B prover off-the-shelf: wire the model-server role to Novita (OpenAI-compatible, `https://api.novita.ai/openai`) instead of self-hosting, keep verifier on Modal. Cheapest entry point per the post.
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

## 9. Test problems & evaluation data (solver)

### 9.1 The difficulty ladder — start at the top (easiest)

These are the benchmarks Qwen2.5-Math / OpenMath-Nemotron report against, so we test on the ground they were tuned for.

| Dataset | Difficulty | Answer type | HF id | Checkable? |
|---|---|---|---|---|
| **GSM8K** | easiest — grade-school word problems | integer (after `####`) | `openai/gsm8k` | trivially (`==`) |
| **MATH-500** | competition, 5 levels, 7 subjects | `\boxed{}` LaTeX | `HuggingFaceH4/MATH-500` | needs symbolic equality |
| **AMC23** | olympiad-lite | integer | `AI-MO/aimo-validation-amc` | `==` |
| **AIME 24/25** | hard headline number | integer 0–999 | `AI-MO/aimo-validation-aime`, `Maxwell-Jia/AIME_2024` | `==` |
| OlympiadBench / Minerva | hardest | mixed | — | fiddly |

**Build order:** GSM8K first (a failure points at the loop — fence parsing, kernel state, stop tokens — not at a stumped model), then MATH Level 1, then up the ladder.

### 9.2 Grading is a second problem hiding behind the first

Integer-answer sets (GSM8K, AMC, AIME) grade with `extract_boxed(...) == gold` — done. **MATH answers are LaTeX**, where `\frac{1}{2}`, `0.5`, and `0.50` are all correct, so a string compare scores right answers wrong. That needs symbolic equivalence: [`math_verify`](https://github.com/huggingface/Math-Verify) (HF) or a sympy normalizer. **So keep the earliest tests on integer-answer sets**, and only add the symbolic checker once the loop itself is trusted.

### 9.3 How to pull them

1. **Hugging Face directly** — ids above, via `datasets.load_dataset(...)`. NB the *original* full `hendrycks/competition_math` was DMCA'd off HF; use **MATH-500** or a lighteval mirror.
2. **NeMo-Skills `prepare_data`** (preferred) — `ns prepare_data gsm8k math aime24 amc23 …` downloads them **and bundles the math grader**, in the prompt/answer format these loops expect. Since we're NeMo-Skills-adjacent, this hands us §9.2's grader for free.

### 9.4 Inline starter staircase (known answers, no dataset needed)

Four self-checking problems, easy → less-easy, all clean integers — for a first run before wiring a loader. All four hand-verified:

| Problem | Answer | Tests |
|---|---|---|
| `Find the remainder when 7^999 is divided by 1000.` | **143** | forces `pow(7,999,1000)` — a real TIR move |
| `Natalia sold clips to 48 friends in April, then half as many in May. How many clips did she sell altogether?` | **72** | the canonical first GSM8K problem |
| `What is the value of \sqrt{36+64} - \sqrt{25-16}?` | **7** | MATH Level-1 flavour |
| `How many positive integers less than 1000 are divisible by neither 5 nor 7?` | **686** | inclusion–exclusion; brute-forceable |

### 9.5 What to add to the repo (TODO)

- `samples.jsonl` — the four problems above (`{"problem", "answer"}`), plus a small `load_gsm8k()` / `load_math500()` stub using `datasets`.
- `eval.py` — run `solver_loop.solve` over a set, pull `extract_boxed`, grade integers with `==`; leave a commented `math_verify` hook for MATH. Report accuracy (and maj@k accuracy via `fanout.py`).
- Wire `eval.py` into §5 step 2.

## 10. Parallel sampling / maj@k width

**The lever.** maj@k wants `k` completions per problem. The efficient form is **server-side parallel sampling** — `n>1`: one request returns `k` choices, and the engine computes the prompt prefill *once* and decodes the `k` sequences in parallel against a shared KV cache. Distinct from "parallel/speculative decoding" (Medusa etc.), which is a *single-sequence latency* trick — we want the former.

**Who offers it** (verified against provider docs, 2026-06):

| Path | `n>1`? | Efficient (shared prefill)? | Marginal cost of the k samples |
|---|---|---|---|
| **Self-host vLLM / SGLang** | ✅ first-class (`SamplingParams.n`; SGLang RadixAttention fork) | ✅ prefill once, decode n on shared KV | GPU-time only — the real saving |
| **Novita** | ✅ documented, `n` 1–128 | server-side, opaque | billed per output token for all n |
| **Together / Fireworks / OpenAI** | ✅ documented | opaque | billed for all n (latency-only win) |
| **OpenRouter** | ❌ silently dropped → 1 choice | — | n/a |
| **Featherless** | ❌ no chat `n` (prompt-array on `/v1/completions` only, counts against concurrency) | — | flat-rate but **concurrency-capped** |

Sources: vLLM `SamplingParams` (https://docs.vllm.ai/en/latest/api/vllm/sampling_params.html) + APC; SGLang RadixAttention (https://lmsys.org/blog/2024-01-17-sglang/); Novita `n` (https://novita.ai/docs/api-reference/model-apis-llm-create-chat-completion); OpenRouter drop-to-1 (https://github.com/OpenRouterTeam/openrouter-runner/issues/99); Featherless concurrency tiers (https://featherless.ai/docs/concurrency-limits — Premium = 4).

**The catch.** Shared prefill only amortizes the *prompt*. Decoding `k` long CoT-plus-code traces is never shared and never gets cheaper; for maths the trace dwarfs the prompt, so even at best `n>1` buys latency/concurrency, not cost. On per-token APIs you pay for all `n` outputs regardless.

**What this means for `fanout.py`.** The current default (Featherless, `ENDPOINT_CONCURRENCY = 4`) is the *weakest* case: no `n`, and maj@k width is bounded by the plan's concurrency (Premium = 4 — `.map` wider gets 429s). Options, cleanest first:
1. **Self-host `serve.py` (vLLM)** — `n=k` in one request (or rely on `@modal.concurrent` batching). The only path with `n>1` *and* the prefill saving. The repo already supports it via `SOLVER_BASE_URL`.
2. **Novita for the solver** *iff* it lists the Qwen2.5-Math models (catalogue check — we have a Novita key for the prover). `n` ≤128 sidesteps the per-request concurrency cap; still per-token.
3. **Stay on Featherless** for single-shot dev; accept maj@k is concurrency-bound to the tier.

## 11. Supporting generalist reasoners (a second orchestrator contract)

**Why.** The specialist solvers (OpenMath-Nemotron, AceMath) are Featherless-or-DIY only (§ off-the-shelf), and the 2026 open math leaderboards are increasingly topped by *general* frontier reasoners — DeepSeekMath-V2 (Apache-2.0, NL self-verifying proofs, https://huggingface.co/deepseek-ai/DeepSeek-Math-V2), Qwen3-235B-Thinking, Kimi-K2 — which *are* widely rentable per-token. So we'll likely want the loop to drive a generalist. It can't, as written.

**What breaks.** `solver_loop.py` assumes the Qwen2.5-Math TIR contract: hand-rolled `<|im_start|>` template on `/v1/completions`, stop on the ```` ```output ```` fence, splice the result inline. Generalists differ on two axes:
1. **Reasoning is a separate channel**, not inline — a `reasoning_content` field (DeepSeek/Qwen3/Kimi) or a hidden/summarized trace (OpenAI o-series, Gemini). You must NOT feed it back as the answer, and some APIs 400 if you echo it (DeepSeek) or break multi-step tool loops if you *don't* resend it (Kimi-K2).
2. **Tool use = the `tools`/`tool_calls` protocol**, not fence-splice: the model emits a structured JSON call, you execute and return a `role:"tool"` message; loop while `finish_reason == "tool_calls"`, stop on `"stop"`.

**Design: a pluggable contract, executor unchanged.** The `Kernel` / remote `Executor` (`run(code) -> str`) stays exactly as-is. Add a `mode` to the orchestrator:

| Mode | For | Loop |
|---|---|---|
| `fence` (current) | Qwen2.5-Math, OpenMath-Nemotron (maths-tuned) | stop on `output` fence, splice inline |
| `tools` | generalists with a code executor | register a `run_python` tool; loop on `tool_calls` → execute in `Kernel` → append `role:"tool"`; stop on `"stop"` |
| `cot` | generalists, no executor | sample n, `extract_boxed`, vote — no executor in the loop; often competitive for competition maths |

**Start with `cot`.** It needs *no harness at all* — one chat call, no executor, reuse `extract_boxed`, vote across `n` samples — and a strong reasoner is competitive-or-better this way on most competition maths. Only add `tools` mode when problems need exact computation the model fumbles in its head (the `7^999 → 43` class of error). Sequence: `cot` first (free), `tools` only if accuracy demands it. We deliberately keep execution on *our* `Kernel` either way — no provider-hosted code-interpreter (the whole repo exists to avoid handing execution to the provider).

Switch to the **chat endpoint** (`chat.completions`, or OpenAI **Responses** API for o-series/GPT-5) — drop the hand-rolled completion template; the server applies the model's chat template and parses `reasoning_content`/`tool_calls` into fields. `extract_boxed` is reused for `cot` and as a fallback.

**Per-model gotchas (verified, 2026-06):**
- **DeepSeek** (R1/V3.x): reasoning in `reasoning_content`; **strip it before echoing the turn back** (else HTTP 400). R1 had no function calling; V3.1+ added it, V3.2 added tools-in-thinking. https://api-docs.deepseek.com/guides/reasoning_model
- **Qwen3**: `reasoning_content` (vLLM `--reasoning-parser deepseek_r1`); native tools via `--enable-auto-tool-choice --tool-call-parser hermes`; thinking + tools combine. https://qwen.readthedocs.io/en/latest/framework/function_call.html
- **Kimi-K2**: `reasoning_content`; **must resend it in tool-call history** or multi-step breaks; built for long-horizon agentic loops. https://github.com/MoonshotAI/Kimi-K2/blob/main/docs/tool_call_guidance.md
- **OpenAI o-series / GPT-5.x**: reasoning hidden (summary only); use the **Responses API**; pass reasoning items back between tool calls (`previous_response_id` / `encrypted_content`); built-in code interpreter available. https://platform.openai.com/docs/guides/reasoning
- **Gemini 2.5/3**: thought summaries (`includeThoughts`), **thought signatures must round-trip** for function calling; native `code execution` tool. https://ai.google.dev/gemini-api/docs/function-calling

**Token budget.** Reasoning shares `max_tokens` with the answer; a model can spend the whole budget reasoning and return `content=""` (200 OK, billed). Reserve generously (OpenAI suggests ≥25k for reasoning models). This is the repo's existing `MAX_TOKENS` concern, amplified.

**Status:** design only — not built. `fence` mode (current `solver_loop.py`) is the spine; `tools`/`cot` modes are the generalist extension, parallel in spirit to how the prover extends the solver.
