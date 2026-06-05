# EXPLORER_PLAN — the multi-hypothesis fan-out explorer (handoff spec)

Self-contained handoff for a fresh instance. The third affordance, distinct from the chat
**workbench** (`WORKBENCH_PLAN.md`) and the batch **audition** (`audition.py`): a surface for
**throwing many hypotheses out at once and exploring the spread** — launch a sweep, watch a grid
fill, cluster the answers, drill into transcripts, re-run, branch.

Read `PLAN.md` §1–2 (trust ladder, the event envelope) and the "parallelism" framing in §3 below.

## 0. Why this exists (the gap)

We have the *engine* for many-hypotheses-at-once (the audition fans out model × strategy × problem
× sample via `asyncio`, resumable + instrumented) but no *surface* to **explore** its output: the
audition is a batch CLI → leaderboard, and the workbench is single-thread chat. A chat window is
structurally the wrong shape for parallel exploration. This is the missing surface.

Three shapes of use, one engine:

| affordance | shape | this doc |
|---|---|---|
| **Solve** (`shim.py` chat) | one problem · one model · streamed | `WORKBENCH_PLAN.md` |
| **Explore-one** (the `…-deep` rung) | one problem → k samples → distribution | `WORKBENCH_PLAN.md` |
| **Explorer** (this) | **{prompts} × {lineup} × k → a grid you explore** | here |

## 1. What exists (reuse) / what's missing (build)

**Reuse:**
- `solver_loop.solve_one_async(problem, *, strategy, model, provider, temperature, seed,
  max_tokens, client) -> {boxed, transcript, completion_tokens, truncated, elapsed_s, ttft_s,
  decode_tok_s, error}` — **one chain, with its transcript.** This is the per-sample primitive.
- `eval.grade` / `eval._norm_latex` — answer normalization, reused to **cluster** equivalent answers.
- `eval.load_data` (datasets) + `audition.load_contenders` (`contenders.jsonl`, the lineup).
- The resumable JSONL persistence pattern in `audition.py` (kill-safe rows).
- `providers.PROVIDERS` + the fetched prices (for the $ column).

**Missing (build):**
- A headless **`explore.py`**: fan out k samples per (prompt × contender), keep **all** samples +
  transcripts, cluster by answer, return a grid. (`eval.solve_graded_async` votes and *discards*
  the per-sample detail — the Explorer needs the distribution, not just the winner.)
- A **`explorer.py`** marimo surface over it.

## 2. Decisions (locked)

1. **marimo, not Jupyter** (Dan's preference, and the better fit): reactive cells, notebooks are
   plain `.py` (git-friendly, reviewable), built-in UI (`mo.ui.table/dropdown/multiselect/slider/
   run_button`), and `mo.md` renders LaTeX (KaTeX) for transcripts. `marimo edit explorer.py` to
   explore; `marimo run explorer.py` serves it as an app. Add `marimo` + `polars` to deps.
2. **Thin reactive layer over a headless engine.** `explorer.py` calls `explore.py`; all fan-out,
   clustering, persistence live in `explore.py` (importable, testable headless, no marimo import).
   The notebook is only the surface.
3. **Cell = (prompt × contender); drill-in = its k samples.** The grid is the overview (voted
   answer · agreement · $ · time per cell); selecting a cell opens the **distribution** (answers
   clustered) + each sample's transcript.
4. **Cluster by normalized answer** (`_norm_latex` + int/decimal), reusing `eval`'s logic. Caveat:
   symbolic-equal-but-different-form answers may split into separate clusters (math_verify is
   pairwise, too expensive for live clustering) — acceptable for "see the spread".
5. **Local `asyncio` is the fan-out; Modal is deferred** (see §3). v1 is rented-generalist → local.
6. **Reuse `contenders.jsonl`** as the lineup (same swappable source as audition/workbench).
7. **Resumable + kill-safe**: a sweep persists per-cell to `results/explore-<id>.jsonl`; the
   notebook can reload a prior sweep instead of re-running.

## 3. Fan-out & where it runs (the parallelism answer)

**We did not lose Modal fan-out — we matched the primitive to the workload.** The Modal-`.map`
emphasis came from the TIR/prover frame, where the bottleneck is a **per-chain sandbox** (kernel /
Lean verifier). Generalist `cot`/`self_verify` run **no code** and the model is **rented**, so the
parallel unit is just an HTTP call: `asyncio.gather` + a `Semaphore` at the provider's rate limit
*is* the complete fan-out. **Modal can't even raise that ceiling** — more containers hit the same
per-account rate limit. Modal re-enters only when **you own a heavy/stateful per-call thing**:

| regime | per-call unit | fan-out primitive |
|---|---|---|
| generalist `cot`/`self_verify` (rented) | an HTTP call | **local `asyncio.gather` + Semaphore** ✅ now |
| **prover** Pass@k (Phase C) | a Lean verifier sandbox | **`modal.Sandbox.map`** — `fanout.py`'s real future |
| **`tools`** rung | a code sandbox | Modal / E2B per call |
| **self-hosted model** (`serve.py`) | your GPU | Modal serve + `n>1` shared prefill |

So: **`explore.py` takes a `backend` knob** — `"local"` (asyncio, default) or `"modal"` (map over
a Modal function; deferred until the prover/tools rungs exist). And **`fanout.py` is stale** — the
Modal `.map` template still wired to the dropped sync-TIR path; **repurpose it as the prover's
Pass@k backend**, don't wire it to the generalist Explorer (which doesn't need it).

## 4. Build order

### V1 — headless engine (`explore.py`)
1. `async def explore_cell(prompt, *, provider, model, strategy, k, max_tokens, sem) -> Cell`:
   `asyncio.gather` k `solve_one_async(... temperature=0.6, seed=s ...)`; cluster by normalized
   boxed; return `{prompt, contender, voted, agreement, clusters, samples[...], cost, mean_time,
   mean_ttft}`. `samples[i] = {boxed, transcript, tokens, ttft, decode, error}`.
2. `async def explore(prompts, lineup, *, k, concurrency, max_tokens, backend="local",
   on_cell=None) -> list[Cell]`: cross-product, shared `Semaphore`, `as_completed` with an
   `on_cell` callback (so the notebook can fill the grid live) + append each cell to
   `results/explore-<id>.jsonl`.
3. `to_table(cells) -> polars.DataFrame` (grid columns: prompt, contender, voted, agreement,
   n_clusters, $, s, ttft).

### V1 — marimo surface (`explorer.py`)
4. Controls (`mo.ui`): prompt source (dataset dropdown | pasted list | parametric template), lineup
   `multiselect` (from `contenders.jsonl`), k slider, concurrency, max_tokens; a `run_button` gating
   the expensive sweep (don't re-fan-out on every tweak).
5. Run the sweep in an `await` cell (marimo supports top-level await); show a `mo.status.progress_bar`.
6. **Grid:** `mo.ui.table(to_table(cells))`, agreement/correctness colour-coded; row selection →
7. **Drill-in:** the selected cell's **clusters** (e.g. "5/8 → \(42\) · 2/8 → \(41\) · 1 ∅") and
   each sample's transcript via `mo.md` (LaTeX renders).

### V1.1 — exploration verbs
8. **Re-run** a cell (wider k, different model) in place; **branch** (take a sample's answer and ask
   a follow-up across the whole lineup); **reload** a prior `results/explore-*.jsonl`.
9. Capture the **thinking trace**: extend `solve_one_async` to collect `thinking_delta` (now emitted
   by `strategies.py`) so drill-in can show the model's *working*, not just the answer.

### V2 — Modal backend
10. `backend="modal"`: `explore_cell` runs on a `modal.Function`/`Sandbox` per cell — needed only
    once the prover/tools rungs (sandbox-per-call) exist; reuse the repurposed `fanout.py`.

## 5. Concrete contracts

- **Cell record** (one per prompt × contender; persisted as one JSONL line):
  `{prompt, provider, model, strategy, k, voted, agreement, clusters: {answer: [seed...]},
   samples: [{seed, boxed, transcript, tokens, ttft, decode, error}], cost, mean_time, mean_ttft}`.
- **`explore_cell` / `explore`** signatures as in §4. Keep `explore_cell` the primitive; optionally
  refactor `eval.solve_graded_async` to reuse it (DRY) — not required.
- **Clustering key:** `eval._norm_latex(boxed)` after int/decimal normalization; `None`/`""` → the
  `∅` (no-answer) cluster.
- **Prompt sources:** a benchmark slice (`eval.load_data`), a pasted list, or a **parametric
  template** (`"Is {n}^2+1 prime?"` over `n=2..20`) — exploration's killer case; spec a tiny
  `expand_template(tmpl, ranges)`.

## 6. Testing checklist

1. [ ] **`explore_cell` headless** with the `AsyncFakeClient` (from `tests/test_strategies.py`):
   scripted k samples with mixed answers → assert clusters + agreement + per-sample transcripts.
2. [ ] **Clustering** groups `\dfrac{1}{2}` with `\frac{1}{2}` and `0.5`; `None` → `∅`.
3. [ ] **`explore`** cross-products correctly, persists one JSONL line per cell, resumes (skips
   cells already present), bounded by `concurrency`.
4. [ ] `to_table` shape/columns.
5. [ ] Notebook smoke: `marimo run explorer.py` loads; a tiny live sweep (samples set, 2 contenders,
   k=2) fills the grid; drill-in renders LaTeX.

## 7. Open questions

- **marimo async ergonomics:** live grid-fill (`as_completed` + reactive state) vs gather-then-show.
  Lean: `on_cell` callback updating `mo.state`, gated by a run button so tweaks don't re-fan-out.
- **Persistence/reload UX:** a sweep id; "load prior sweep" dropdown over `results/explore-*.jsonl`.
- **Transcript rendering** in marimo: confirm `mo.md` KaTeX handles streamed-model LaTeX (reuse
  `streaming.normalize_delimiters`; these are *complete* transcripts, so no FOIM buffering needed).
- **Parametric templates:** how rich? (ranges, products, a Python expr). Keep v1 to `{var}` over lists.
- **Modal backend interface** (V2): the `explore_cell`-on-Modal contract; share with prover Pass@k.
- **Relationship to `audition.py`:** keep the CLI for batch/CI numbers; the Explorer is the
  interactive sibling over the same engine. Consider having `audition.py` import `explore.py`'s
  primitive so they don't drift.

## 8. File map

- **`explore.py`** — NEW: headless fan-out engine (`explore_cell`, `explore`, clustering, `to_table`,
  `expand_template`), `backend` knob. No marimo import.
- **`explorer.py`** — NEW: the marimo surface (controls, grid, drill-in).
- **`solver_loop.py`** — small (V1.1): have `solve_one_async` optionally capture the `thinking` trace.
- **`fanout.py`** — repurpose for prover Pass@k (Modal); note it's not the generalist path.
- **`pyproject.toml`** — add `marimo`, `polars`.
- **Reuse unchanged:** `solver_loop.solve_one_async`, `eval` (grade/normalize/loaders),
  `contenders.jsonl`, `providers.py`, the resumable-JSONL pattern.

## 9. Definition of done (V1)

`marimo edit explorer.py`: pick a prompt set + a few contenders + k, hit run, watch a grid fill;
each cell shows the voted answer · agreement · $ · time; click a cell to see the **answer
distribution** and read each sample's transcript (LaTeX rendered). The surface for exploring a
*space* of hypotheses — the parallel-research tool the chat workbench can't be.
