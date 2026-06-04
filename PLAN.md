# PLAN — a personal maths workbench: checked, confidence-rated answers

Results-oriented handoff. **Supersedes** the "solver-first TIR spine" framing (git ≤ `07b7e5d`).
The pivot in one line: **the fine-tuned specialists lost; a rented generalist + verification is
the product.** What a working mathematician wants from this repo is not a model but an
*affordance* — throw a hard problem in, get an answer **with a trustworthiness signal**, and
climb to a stronger check only when the answer warrants it.

## 0. What changed and why (read this first)

- **Specialist TIR underperforms — drop the attachment.** This repo's own result: Qwen2.5-Math
  7B *and* 72B answer `7^999 mod 1000` as `43`, maj@8 unanimously wrong — a systematic
  "won't trust the tool" failure. Meanwhile the 2026 open leaderboards (MathArena, etc.) are
  topped by *generalist* reasoners — Kimi K2.6, Qwen3-235B-Thinking, DeepSeek, DeepSeekMath-V2
  — which we can rent per-token. So `tir_fence` drops from "the spine" to "one auditionable
  contender, deletable if it loses." We are **not sentimental** about that code.
- **The product is a workbench, not a benchmark harness.** The audition (§3) is *instrumental*
  — it picks the engine. The thing we keep and use is the workbench (§4): the existing Open
  WebUI chat surface (`shim.py`) wired to whichever (model × rung) wins.
- **The organizing idea is a trust ladder (§1).** Solver and prover are not two products; they
  are two rungs on one ladder of trust-vs-friction. "Is the Lean block worth it?" becomes "how
  far up the ladder does *this* problem need me to climb?" — usually rung 1–3, not 4.
- **Results discipline.** Build nothing we can't justify with a number. The only thing built
  before the numbers is the audition. Lanes are killed by data, not taste.

## 1. The trust ladder (the spine of the new design)

The affordance a working mathematician actually wants is **"check this"** more than "answer
this": you can often do the maths; what you want offloaded is grunt-work *plus confidence*.
Every rung above 0 is a checker — that is the specialist hook a chatbot lacks.

| Rung | Mechanism | Friction | What it buys | Infra | Code |
|---|---|---|---|---|---|
| 0 | one CoT sample | none | a guess (= a chatbot) | token fountain | — |
| 1 | **maj@k self-consistency** | low | "k independent samples agree" | fountain `n`, or `fanout.py` | `strategy=cot` + `--k` |
| 2 | **self-verification** (NL critic) | low–med | the model hunts its own holes (DeepSeekMath-V2 does this natively) | fountain | `strategy=self_verify` |
| 3 | **tool-checked** (runs SymPy/numerics) | med | exact computation, no hallucinated arithmetic | our executor (borrow smolagents) | `strategy=tools` (gated) |
| 4 | **Lean compiler** | high | unfakeable — but you must *state* it formally | our verifier (Modal + Kimina) | `strategy=prover` (gated) |

`tir_fence` (the specialist TIR loop) sits beside rung 3 — a code-checked solver, but with a
maths-tuned model that empirically won't trust its own tool. Kept as a contender, not the trunk.

## 2. Architecture — one verb, pluggable rung

Three roles stay separable (model server / executor-or-verifier / orchestrator) — unchanged,
and still the pedagogical point. **What's new:** the *rung* is a parameter, and every strategy
emits the **same streaming event envelope** (`streaming.py`: `reasoning_delta` / `code` /
`tool_result` / `final_answer` / `error`). That envelope is the universal seam — a new strategy
that emits it inherits `eval.py`, `fanout.py`, **and** the chat shim with no changes.

```
solve(problem, *, strategy, model, provider, executor, k) -> answer (+ confidence)
        strategy ∈ {cot, self_verify, tir_fence, tools, prover}   # the rung
        provider  → providers.py registry                          # model-server axis
        executor  → Kernel (tools/tir) | Lean verifier (prover) | none (cot/self_verify)
```

The harness owns the **problem set + grader + fan-out + verifier**; **contenders bring their own
driving** — `cot` is a bare chat call; `tools` should *borrow* smolagents rather than re-rolling
six providers' tool-call quirks; an off-the-shelf agent (Hermes→Kimi) is just *one opaque
contender* you could plug in behind `solve()`. We do **not** write a universal model driver.

## 3. Phase A — the audition (BUILD FIRST; the only pre-numbers build)  ← we are here

**Goal:** a table of (model × strategy × provider) → accuracy / cost / agreement on real
benchmarks, so the numbers pick the engine and kill the dead rungs.

**Built this increment** (all green, `tests/test_strategies.py` + existing suite, 24 passed):
- `providers.py` — provider registry (base_url, key envs, `n>1` support). Confirmed: featherless,
  novita, selfhost. Likely/overridable on first 401/404: moonshot (Kimi), openrouter, deepinfra.
- `strategies.py` — `cot` (rung 1) and `self_verify` (rung 2): chat-endpoint, no executor, emit
  the shared envelope; handle `reasoning_content` (read the answer from `content`, fall back to
  reasoning only when content has no `\boxed{}`). `tools` raises NotImplemented (gated).
- `solver_loop.solve_stream(..., strategy=, provider=)` dispatches non-fence rungs; the
  `tir_fence` path is byte-identical (verified by the unchanged tests).
- `eval.py --provider --strategy` + integer-answer loaders (gsm8k, amc23, aime24).

**Run the audition:**
```bash
# incumbent specialist (metered tokens, local kernel)
eval.py --provider featherless --model OpenMath-Nemotron-32B --strategy tir_fence  --data aime24 --k 8
# generalist, chain-of-thought (rung 1)
eval.py --provider moonshot    --model kimi-k2.6                       --strategy cot         --data aime24 --k 8
# generalist, self-verification (rung 2)
eval.py --provider deepinfra   --model Qwen/Qwen3-235B-A22B-Thinking   --strategy self_verify --data amc23  --k 4
```
Then the **lane-kill comparison**: the same top model in `cot` vs `self_verify` (vs `tools` once
built). If tools don't beat plain CoT for a SOTA generalist, that rung dies — as it did for the
specialist. Generalist runs need a fat token budget (thinking shares `max_tokens`); eval defaults
to 16384, raise with `--max-tokens` if you see `∅ no_answer`.

**Next increments in Phase A (small, in order):**
1. **Cost + agreement reporting.** Surface `completion_tokens` (the per-token $ proxy; flat-rate
   Featherless still uses wall-time) and the maj@k vote margin ("5/8 agree") as the confidence
   signal. Needs `solve` to return usage alongside the boxed answer (today it returns the
   transcript only).
2. **MATH-500 grading** via `math_verify` (LaTeX equality) — `eval.py grade()` has the hook.
3. Optional: drive headline numbers through **lighteval / lm-eval-harness / NeMo-Skills** for
   contamination-checked, publishable figures; keep `eval.py` for the staircase + dev loop.
   (Don't re-implement graders — 2026 best practice is to plug into one.)

## 4. Phase B — the workbench (build around the winner)

Wire the winning (model × rung) behind the chat surface that **already exists** (`shim.py` +
`streaming.py` + Open WebUI, `OPEN_WEBUI.md`). The shim consumes the same envelope, so `cot` /
`self_verify` render today with **no shim change**. The real new work is **surfacing trust**:
show the confidence signal (agreement / self-verify verdict), not just the answer — the "check
this" UX. This is the thing you actually use. (`shim.py` is currently hard-wired to a local
`Kernel`; generalize it to pass `strategy`/`provider` so the workbench serves the winner.)

## 5. Phase C — the prover spike (GATED parking lot)

**Do NOT build until both gates pass:** (a) you can name a problem you want *bulletproof*, and
(b) you have a usability answer — no chat UI renders Lean proof state, so either embed
[`lean4web`](https://github.com/leanprover-community/lean4web) beside the chat or stay at rung
2–3 and never show Lean. Honest cost: the friction is **stating** the theorem formally
(autoformalization is 55–75% faithful — it may prove the *wrong* theorem), not proving it.

**Cheapest try-before-you-buy (an afternoon, no GPU):**
- model: DeepSeek-Prover-V2-671B on **Novita** (metered) *or* a generalist (Kimi/Opus) with a
  "close this `sorry`" prompt — the 2026 surprise is generalists are now competitive provers
  (Claude Opus one-pass Lean, Mistral's Leanstral, SorryDB).
- verifier: `modal.Image.from_registry("projectnumina/kimina-lean-server:2.0.0")`, `POST
  /api/check` — exact contract + the Sandbox-restart pattern in `PROVER_RESEARCH_ADDENDUM.md`.
- one hand-written MiniF2F statement. **Feel the friction, then decide.**

When built, `prover` is just another strategy: executor = the Lean verifier, "vote" = first
candidate that compiles (Pass@k, which is cheap because the compiler is unfakeable).
`prover_loop.py` + `verifier/` are the existing skeleton.

## 6. Reference (verified — reuse when a rung earns it)

### 6.1 Providers / parallel sampling — who offers `n>1` (verified 2026-06)
The lever for maj@k is server-side `n` (k samples, prompt prefill shared once). Only self-hosting
gets `n>1` **and** the prefill saving; per-token APIs bill all `n`; shared prefill amortizes only
the (short) prompt, so maj@k buys accuracy, not a discount.

| Path | `n>1`? | Efficient (shared prefill)? | Notes |
|---|---|---|---|
| **Self-host vLLM/SGLang** (`serve.py`) | ✅ (`SamplingParams.n`) | ✅ prefill once, decode n | GPU-time only — the real saving; cheapest wide maj@k |
| **Novita** | ✅ (`n` 1–128) | server-side, opaque | per-token; also hosts DeepSeek-Prover-V2-671B |
| **Together / Fireworks / OpenAI** | ✅ | opaque | billed for all n (latency win only) |
| **OpenRouter** | ❌ silently → 1 | — | router; set data-policy for no-train |
| **Featherless** | ❌ (no chat `n`) | — | flat-rate but concurrency-capped; the specialists live here |

### 6.2 Per-model generalist gotchas (verified 2026-06)
Single-shot `cot`/`self_verify` sidestep most of these (we never echo reasoning back); they bite
when building `tools`/multi-turn.
- **DeepSeek** (R1/V3.x): reasoning in `reasoning_content`; **strip before echoing** (else HTTP
  400). V3.1+ added function calling. <https://api-docs.deepseek.com/guides/reasoning_model>
- **Qwen3**: `reasoning_content` (vLLM `--reasoning-parser deepseek_r1`); tools via
  `--enable-auto-tool-choice --tool-call-parser hermes`; thinking+tools combine.
- **Kimi K2.6**: `reasoning_content`; **must resend it in tool-call history** or multi-step
  breaks; built for long-horizon agentic loops. OpenAI/Anthropic-compatible on `platform.moonshot.ai`.
- **OpenAI o-series / GPT-5.x**: reasoning hidden; use the **Responses API**; pass reasoning
  items back between tool calls. (Not wired — needs a non-chat endpoint; add as a provider later.)
- **Gemini 2.5/3**: thought summaries; **thought signatures must round-trip** for function calling.
- **Token budget:** reasoning shares `max_tokens` with the answer; a stingy cap returns
  `content=""` (200 OK, billed). Reserve generously (eval defaults generalists to 16384).

### 6.3 Datasets & grading (the audition's problem sets)
Difficulty ladder, easiest first (test on the ground the models report against):
GSM8K (`openai/gsm8k`, integer after `####`) → MATH-500 (`HuggingFaceH4/MATH-500`, LaTeX →
needs `math_verify`) → AMC23 (`AI-MO/aimo-validation-amc`, int) → AIME24/25
(`AI-MO/aimo-validation-aime`, int 0–999, the headline). Integer sets grade with normalized
`==` (a failure points at the loop, not the grader); MATH needs symbolic equality. `eval.py`
loaders + the `math_verify` hook in `grade()`. NeMo-Skills `prepare_data` bundles datasets +
grader if we want the standard harness.

### 6.4 Prover I/O + verifier image → `PROVER_RESEARCH_ADDENDUM.md`
Frozen, verified-against-source: prebuilt Kimina image (no multi-hour build), `/api/check`
contract, DeepSeek-/Goedel-Prover-V2 prompt template, version-match risks, Novita 671B route.

## 7. File map

- `providers.py` — **NEW**: provider registry (model-server axis); `make_client(provider)`.
- `strategies.py` — **NEW**: `cot` / `self_verify` generalist rungs; `tools` gated. Chat endpoint, shared envelope.
- `solver_loop.py` — `tir_fence` engine + the strategy dispatch + the shared envelope; local `Kernel`.
- `eval.py` — the **audition runner**: (model × strategy × provider) → accuracy/time (cost+agreement next).
- `fanout.py` — Modal `.map` fan-out: maj@k (solver) / Pass@k (prover) — the confidence axis at scale.
- `shim.py` + `streaming.py` — the **workbench surface** (Open WebUI bridge + FOIM delimiter buffer).
- `serve.py` — optional self-host vLLM (the only cheap wide-maj@k path; `n>1` shared prefill).
- `prover_loop.py` + `verifier/` — Phase C skeleton (gated). `executor/modal_executor.py` — remote heavy-compute kernel (tools at scale).
- `tests/` — `test_strategies.py` (new seam, network-free) + `test_events.py` / `test_shim.py` (envelope + UI).

## 8. Status

Phase A seam **built and tested** (network-free): provider registry, `cot`/`self_verify`,
strategy dispatch, audition loaders. `tir_fence` path unchanged (24 tests pass). **Next action:**
run the audition (commands in §3) with real keys to get the engine-and-rung table, then Phase B
(workbench around the winner). Phase C (prover) stays gated until a bulletproof use-case + a UI
answer exist.
