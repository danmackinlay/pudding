# WORKBENCH_PLAN — Phase B: the swappable solver workbench (handoff spec)

Self-contained handoff for a fresh instance. Builds Phase B from `PLAN.md` §4: turn the audition
engine into a thing a working mathematician *uses* — throw a hard problem in, get a **checked,
confidence-rated** answer, in a chat UI, with a **model/rung picker** so nothing is locked in.

The audition (`audition.py`/`eval.py`) is the benchmark harness; this is the **product**. Read
`PLAN.md` (esp. §1 the trust ladder, §2 the envelope, §4 Phase B) first.

## 0. Why this, why now (the judgments)

- **UX before prover.** The prover (`PLAN.md` §5) stays gated — it needs a named bulletproof
  use-case *and* a usability answer, neither of which exists yet. The workbench is the product
  Dan opens; it builds on already-verified code; and **using it is how he'll discover whether he
  ever needs the compiler** (where soft verification stops being enough). So: this first.
- **Don't ship a chatbot — ship "check this".** A generalist in a chat window already exists at
  kimi.com. The distinctive affordance is **verification surfaced in the UI**: the self-verify
  verdict and maj@k agreement (the trust ladder, rungs 1–2). If the build only streams an answer,
  it has missed the point.
- **The picker *is* the swappability** Dan asked to keep. Expose each (model × rung) as a
  selectable "model" in Open WebUI; swapping the dropdown is climbing the trust ladder
  (`deepseek-cot` → `kimi-verify` → `kimi-deep`). No "winning model" baked in.
- **The event envelope is the seam, so Phase B is small.** Both the (sync) TIR loop and the
  (async) generalist strategies already emit the *same* `streaming.ev` events; `shim.py` is just
  an envelope→Markdown renderer. The real work is (a) async dispatch by picker choice and (b)
  surfacing trust the envelope already mostly carries — not a rewrite of the engine.

Non-goals here: the Lean prover (Phase C, gated), the `tools` rung (gated — and the only thing
that reintroduces code-execution; generalist `cot`/`self_verify` run **no** model-written code,
so the workbench has no sandbox/security surface), and multi-turn chat (stateless v1).

## 1. What already exists (reuse; do not rebuild)

- **`shim.py`** — the file you mostly rewrite. Today: *sync*, TIR-only, one hard-coded model
  (`tir-solver`), stateless-per-message, renders the envelope via `_render_pieces` (FOIM buffer +
  fences + a time/token footer), with streaming (SSE) and non-streaming paths. The renderer logic
  is good; the engine binding and the single-model assumption are what change.
- **`streaming.py`** — `ev(type, **payload)`, `LatexSafeBuffer` (the FOIM delimiter buffer so
  `\boxed{`/`$…$` never flash mid-stream), `normalize_delimiters` (`$…$`→`\(…\)` for Open WebUI).
  **Reuse verbatim.**
- **`strategies.py`** — `async def cot_stream / self_verify_stream / generalist_stream`, async
  generators that yield the envelope (`reasoning_delta`, `final_answer`, `error`). This is the
  interactive engine for generalists. `self_verify_stream` already emits a
  `reasoning_delta("\n\n---\n**Verification pass**\n\n")` separator between its two passes.
- **`solver_loop.py`** — sync `solve_stream(...)` (TIR, the only sync streamer); async
  `solve_one_async(...)` (drains to a result dict, no streaming); the envelope. `final_answer`
  carries `{boxed, transcript, elapsed_s, completion_tokens, truncated, ttft_s, decode_tok_s,
  calls}`.
- **`eval.py`** — `async solve_graded_async(problem, k, model, *, strategy, provider, …, sem)` →
  `{pred, tokens, agreement, ttft, decode, truncated, error}`. **Reuse for maj@k-deep** (don't
  re-implement voting).
- **`providers.py`** — `make_async_client(provider)`, `PROVIDERS`. Fetched OpenRouter out-prices:
  deepseek-v4-pro $0.87/M, kimi-k2.6 $3.42/M, qwen3.7-max $3.75/M (for the cost footer).
- **`contenders.jsonl`** — the swappable lineup `{provider, model, strategy, label}`. **Reuse as
  the picker source** (single source of truth for both audition and workbench).
- **The envelope (the seam):** `reasoning_delta{text}` · `code{lang,code}` ·
  `tool_result{output}` · `final_answer{…}` · `error{message}`. Consumers dispatch on `type`.

## 2. Architecture decisions (locked)

1. **Async-first shim.** `StreamingResponse` takes an async generator; generalist strategies are
   native async. Make `_render_pieces` and the SSE path `async`.
2. **Generalists are the workbench; TIR is an optional bridge.** `cot`/`self_verify` (async) are
   the primary picker entries. The sync TIR `solve_stream` can appear via a thread→queue async
   adapter (`_aiter_sync`) — ship it last, or skip; the specialist is the demoted incumbent.
3. **Picker = the lineup, slugified.** Load `contenders.jsonl` (override `WORKBENCH_LINEUP`).
   Each row → a `/v1/models` entry with id = slug(label) (or an explicit `"id"` field). Map
   slug → `(provider, model, strategy)`.
4. **The trust ladder is the picker.** Per model, offer rungs: `…-cot` (stream), `…-verify`
   (stream + self-check verdict), `…-deep` (maj@k, agreement panel). Swapping = climbing.
5. **Surface trust, don't bury it.** `self_verify` renders **✓ confirmed {x}** vs **⚠ corrected
   {a}→{b}**; `…-deep` renders **{answer} · agreement m/k**. This is the headline feature.
6. **Show the working.** Generalists put the real derivation in `reasoning_content`, separate
   from `content`; a mathematician wants to read it. Surface it (collapsible/dimmed) — see §3 V1.2
   and the open question.
7. **Stateless per message** (each user turn = a fresh problem), matching the solver's nature.
   Multi-turn is future.
8. **One renderer, model-agnostic.** Keep `_render_pieces` driven only by envelope `type`, so
   every rung/model renders through the same path.

## 3. Build order

### V1 — swappable streaming workbench (the core; ~½–1 day)
1. **Lineup → picker.** Load the lineup; build `MODELS: slug → {provider, model, strategy, label}`.
   `/v1/models` lists the slugs. (For now expose `cot` and `self_verify` rows.)
2. **Async dispatch.** Add `async def stream_events(problem, provider, model, strategy)`:
   - `cot`/`self_verify` → `async for e in generalist_stream(problem, strategy=strategy,
     provider=provider, model=model, max_tokens=16384): yield e`.
   - (optional) `tir_fence` → bridge the sync `solve_stream` via a thread→queue adapter.
   Make `_render_pieces` `async` and `async for e in stream_events(...)`; make the SSE generator
   `async`. The renderer body is otherwise unchanged.
3. **Footer.** `— {label} · ⏱ {elapsed_s}s · {completion_tokens} tok` (+ est-$ once prices wired).
   **Outcome:** an Open WebUI dropdown of `{deepseek-cot, kimi-cot, qwen-cot, kimi-verify}`; throw
   a problem, watch it stream, swap models live.

### V1.1 — the trust surface ("check this")
4. **self-verify verdict.** Add `candidate_boxed` to `self_verify_stream`'s `final_answer`
   (the pass-1 boxed). Shim renders: equal → `✓ self-checked: confirmed \(x\)`; differ →
   `⚠ self-check corrected \(a\)→\(b\)`.
5. **maj@k-deep.** Add `…-deep` picker entries; route them to `eval.solve_graded_async(problem,
   k, model, strategy="cot", provider=…)` (non-streamed — you can't stream a vote). Render a
   panel: `**\(answer\)**  ·  agreement {round(agreement*k)}/{k}` (optionally list dissenters).
   Show a "running k samples…" status while it computes.
6. **est-$ footer** via `prices.json` (model → out $/M); `est$ = completion_tokens × out/1e6`.

### V1.2 — read the working
7. **Thinking channel.** Have `_chat` (in `strategies.py`) emit `reasoning_content` as a distinct
   `ev("thinking_delta", text=…)`; shim renders it in a collapsible/dimmed block (Open WebUI
   `<think>`/reasoning support — confirm the format). Important when `content` is terse and the
   derivation lives entirely in the reasoning channel.

### Optional — TIR in the picker
8. `_aiter_sync(sync_gen)`: run the sync `solve_stream` in a worker thread, push events to an
   `asyncio.Queue`, yield them — so the specialist/TIR rung can share the picker.

## 4. Concrete contracts to add

- **Lineup load + `/v1/models`:** slugify `contenders.jsonl` labels; `MODELS[slug] =
  (provider, model, strategy)`.
- **`stream_events(problem, provider, model, strategy) -> async iterator[event]`** (the dispatch).
- **Envelope additions:** `final_answer.candidate_boxed` (self_verify); new `thinking_delta`
  event (V1.2). Keep them additive — existing consumers ignore unknown fields.
- **`prices.json`:** `{ "moonshotai/kimi-k2.6": {"out": 3.42}, "deepseek/deepseek-v4-pro":
  {"out": 0.87}, "qwen/qwen3.7-max": {"out": 3.75} }`. Reusable by `audition.py` too.
- **maj@k-deep response:** non-stream JSON (or a status-then-result stream) with the agreement panel.

## 5. Testing checklist (in order)

1. [ ] `/v1/models` lists the lineup slugs; Open WebUI shows the dropdown.
2. [ ] **Renderer, headless:** feed `_render_pieces` a *fake* async event stream (reasoning_delta
   chunks with a mid-stream `\boxed{`, a `final_answer`) and assert the FOIM buffer never emits a
   half-open delimiter and the footer is correct. (No network — mirror `tests/test_strategies.py`'s
   fake-client style; this is the cheap, durable test.)
3. [ ] **Live single solve:** pick `deepseek-cot`, ask an AIME problem, get a streamed, LaTeX-
   rendered answer + footer. Swap to `kimi-cot` mid-session; confirm routing.
4. [ ] **self-verify verdict:** a problem where pass-1 is wrong → UI shows `⚠ corrected a→b`; a
   correct one → `✓ confirmed`.
5. [ ] **maj@k-deep:** `kimi-deep` returns the voted answer + agreement m/k.
6. [ ] **est-$ + thinking block** render (V1.1/1.2).
7. [ ] Existing `tests/test_shim.py` still passes (or is updated for the async rewrite).

## 6. Open questions (resolve while building)

- **`reasoning_content` display:** always-collapsible? only when `content` is terse/empty? Models
  differ (some put the whole solution in reasoning, a terse answer in content; others the reverse).
  Lean: show it collapsibly, expanded-by-default if `content` is short. Confirm Open WebUI's
  reasoning/`<think>` rendering.
- **maj@k-deep progress UX:** spinner-until-done vs streamed "sample 3/8…" status chunks.
- **Prices source:** static `prices.json` (simple, drifts) vs pull from OpenRouter `/models` live
  (accurate, a network call at startup). Start static.
- **Workbench lineup:** full `contenders.jsonl` (incl. the specialist) or a curated generalist
  subset via `WORKBENCH_LINEUP`. Let the overnight audition results inform the default.
- **Token budget per rung:** interactive `max_tokens` (16k? higher for hard problems?) trades
  latency for completeness; expose as an effort knob?

## 7. File map (what changes)

- **`shim.py`** — the bulk: async rewrite, picker + `/v1/models` from the lineup, `stream_events`
  dispatch, trust-surface rendering (verdict/agreement), richer footer.
- **`strategies.py`** — small: add `candidate_boxed` to `self_verify` `final_answer`; (V1.2) emit
  `thinking_delta`.
- **`prices.json`** — new (model → out $/M).
- **`OPEN_WEBUI.md`** — update: the picker, the rungs, launch.
- **Reuse unchanged:** `streaming.py`, `providers.py`, `solver_loop.py`, `eval.py`,
  `contenders.jsonl`.

## 8. Definition of done (V1)

Open WebUI, model dropdown of generalist (model × rung) entries; throw a hard problem at any of
them and get a streamed, LaTeX-rendered, **confidence-annotated** answer with a cost/time footer;
swap model or rung live. The thing Dan opens to do maths — not a CLI he invokes, not a chatbot
without a checker.
