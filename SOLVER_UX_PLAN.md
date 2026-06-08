# SOLVER_UX_PLAN — Track 1: the solver studio, deepened (handoff)

Self-contained handoff for a fresh instance continuing **in this worktree** (`claude/phase-b-workbench`,
the fork base / new-main; see `FORK.md`). Continues the arc: Phase A = the audition (`PLAN.md`,
`FINDINGS.md`), Phase B = the chat shim (`WORKBENCH_PLAN.md`), Phase C = the library + studio
(`STUDIO_PLAN.md`). This track does **not** touch the prover — that's Track 2 (`PROVER_PLAN.md`).

Read `STUDIO_PLAN.md` first (the library design, the answer-cluster instrument, decision #9) and
`FORK.md` (the frozen-core contract you must respect). This plan is purely **additive UX** over the
stable `pudding` library — no engine changes, no new core abstractions.

## 0. The thesis (unchanged)
*Write maths, get maths back.* The product is the **answer distribution** (the shape of agreement),
not a single answer. The studio is the reference interactive consumer; the library is the contract.
Everything here is a new lens/widget over the existing `Job` / `view_model` / `Flock`, or a thin
library helper — never a new engine.

## 1. What already exists (reuse; do not rebuild)
- **Library:** `pudding.solve / solve_many / conjecture / falsify / discover / get / recent / pin /
  get_pin`; `Job` (await / `.result()` / `.stream()` / `.cancel()` / `.widen(k)`, store-backed,
  `summary()` / `completed` for poll-based grids); `view_model` / `to_html` / `render(target=)`;
  `flock_view_model`. Headless, opt-in `on_event`. Store is now atomic + corrupt-tolerant.
- **Studio (`studio/app.py`, marimo):** the answer-cluster **board** (stream-fill + kill-switch +
  CoT drill-in + copy-out), the **♻ Reuse** section (recent dropdown / load-by-id / 📌 pin), the
  **▦ Batch** section (`solve_many` → `mo.ui.refresh` timer → live grid + stop-all), the **✨
  Discover** section (conjecture→falsify thinning → survivor→solve). Launch:
  `direnv exec . uv run --extra studio marimo edit studio/app.py` (or `marimo run …`).
- **Tests:** `tests/run_all.py` runs the whole offline suite (66 green at base). Add tests the same
  way (network-free; monkeypatch `jobs.solve_one_async` / `discovery._propose_async`).

## 2. Locked decisions (inherited)
Library-first, zero frontend deps in core (#1); one job layer, three consumers (#2); markdown+LaTeX
interchange, content-addressed `pin` artifacts (#3); marimo = optional `pudding[studio]` consumer
(#4); the library owns the view-model + static render, widgets live in `studio/` (#9). **Do not
violate the `FORK.md` contract.**

## 3. Build order (each is additive; ship + commit independently)

- **P7 — run-management browser. ✅ BUILT.** *Resolved the §4 store question in favour of a **JSON
  index cache** (not sqlite): per-run files stay the source of truth; `store.write` upserts a sibling
  `<jobs>-index.json`; `recent()` lists in O(index) with no full-dir rescan; `store.reindex()`
  self-heals (and `summaries()` heals on first use); `store.delete(id)` drops file + index entry.
  `api.recent(n, *, status, query, sort, desc)` + `api.delete(id)` (exported). Studio ♻ section
  replaced the dropdown with a sortable/searchable `mo.ui.table` + status/query filters + delete-by-id
  that re-lists live (the table depends on a `last_delete` token — no reactive cycle); click a row to
  load. 70 tests green (4 new: delete forgets file+index; recent filter/query; index lists without
  rescan; reindex heals a missing index). sqlite stays a future swap behind the same functions if
  scale demands. **DoD met.**
- **P8 — feed-forward (the generative seam; STUDIO_PLAN P5 #4, where this track meets discovery).**
  Insert a pinned `Result` (or a `Flock` survivor) as **context** for the next `solve`/`conjecture`
  ("given this verified result: …"). A thin library helper (`as_context(result|conjecture) -> str`
  markdown) + a studio "use as context →" button wiring a board/flock row into the problem/context
  box. **DoD:** a result/survivor flows into the next op without copy-paste; provenance carried.
- **P9 — disagreement triage (STUDIO_PLAN §3 — the human-in-the-loop USP).** When the fleet splits
  (`len(clusters) > 1`), surface a **diff of the divergent transcripts at the step they fork** and
  let the expert rule (mark the right cluster → that becomes the answer, recorded). Library: a
  `divergence(result) -> {fork_point, per_cluster_excerpts}` helper over existing attempts. Studio:
  a side-by-side diff widget + a "this one's right" control. **DoD:** for a split result, see where
  chains diverge and pick the correct branch; the choice is persisted with the run.
- **P10 — parametric-template explore grid (STUDIO_PLAN P6 #2 remainder — the exploration killer-case).**
  A template (`"Is {n}^2+1 prime?"` over `n=2..20`, or a benchmark slice via `eval.load_data`)
  expands to a problem set → `solve_many` → the existing live grid, persisted as a **sweep** id.
  Library: `expand_template(tmpl, **ranges) -> list[str]` + sweep persistence (reuses P7's index).
  Studio: a template control feeding the ▦ Batch grid; row-select → drill-in board. **DoD:** sweep a
  parameter, watch the grid fill, drill into any cell's clustered samples, reload the sweep by id.
- **P11 — `widen` / cost-dial controls (the STUDIO_PLAN P2 remainder).** "add k more" button →
  `await job.widen(k)` with live re-render (the library already re-clusters + re-persists; widen now
  diverges correctly even from a k==1 base). A cost dial from `view_model["cost"]` / `est_cost`.
  **DoD:** buy more confidence in-cell and watch the distribution tighten; see the running $.
- **P12 — Quarto publication hop (the lab→publication arc; STUDIO_PLAN §3 decision #4).** Export a
  pinned `Result` to a Quarto-ready `.qmd` fragment (`render(target="quarto")` + `to_html` +
  provenance front-matter). **DoD:** a pinned run re-renders reproducibly into a doc you can publish.

## 4. Open questions (resolve while building)
- **Store/index:** sqlite (queryable, one file) vs an `index.jsonl` manifest (git-friendly, matches
  the audition's `results/*.jsonl`). Probably sqlite local. This is the load-bearing choice for P7/P10.
- **Delete semantics:** hard-delete the file vs a `forgotten` tombstone; cascade to pins?
- **Disagreement diff granularity:** token-level vs step/paragraph-level fork detection (start coarse).
- **Sweep ergonomics:** how much template DSL before it resents being a DSL (keep it `str.format`).

## 5. Non-goals (this track)
The prover, Lean, autoformalisation, Modal, the faithfulness gate — all **Track 2** (`PROVER_PLAN.md`).
No new core abstractions; no changes to the `FORK.md` contract surface without landing them on base
first. No multi-user/hosted SaaS. No bespoke web frontend (use marimo).

## 6. Current state
Base = `6193fbf` (audit taut-up; 66 tests green; tree clean). Studio is fully working
(`marimo edit studio/app.py`). The library is stable and contract-frozen. Start with **P7**.
