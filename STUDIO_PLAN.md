# STUDIO_PLAN — Phase C: the maths studio (library + notebook, async job fan-out)

Self-contained handoff for a fresh instance. Continues the arc: Phase A = the audition
(`PLAN.md` §3, `FINDINGS.md`), Phase B = the swappable chat shim (`WORKBENCH_PLAN.md`). Phase C
turns the engine into **a thing you compute *with***: a `solve()`-style **async function call your
own code can invoke**, and a **markdown-native notebook** where you write maths and get maths
back — with many attempts fanned out on a cloud backend, surfaced as a live in-cell instrument.

Read `WORKBENCH_PLAN.md` (the renderer, the rungs, the trust ladder) and `FINDINGS.md` (why
maj@k agreement is the confidence surface) first. This plan reuses that engine wholesale; the new
work is **a job layer, a library API, a notebook app, and a generative (conjecture) loop.**

## 0. The judgments (why this shape)

- **Library-first; the app is the *reference client*, not the product.** The contract is the
  `pudding` library API + a persistent **job** layer + a **markdown artifact** format — all
  frontend-agnostic. The notebook, the Phase-B chat shim, your scripts, an autonomous agent, and a
  future hosted app are all just *consumers* of the same library. This is the repo's existing ethos
  (separable headless modules; `eval`/`audition`/`shim` are already three consumers of one engine)
  taken to its conclusion. "Function both as a library and as an app" = build the library; the app
  falls out.
- **Headless-first; interactivity is opt-in.** The library must run with *no* frontend in the loop —
  a script, a cron, a batch experiment, or an autonomous agent can spin out and collect maths
  without ever attaching a UI. Every verb has a non-interactive path; the **dependency arrow points
  one way** (frontends → library, never back); marimo is an *optional consumer* (`pudding[studio]`),
  never a core dependency. So `pudding` is equally **a backend an agent/cron calls** and **a human
  instrument** — same engine; the human merely opts into watching and steering.
- **The job layer is the one seam.** "A `solve()` call, possibly async" and "a board of many
  cloud attempts" and "the library API" are **the same object** seen three ways: submit a spec →
  get a persistent `Job` → stream the multiplexed attempt-envelopes → reduce → persist → notify →
  cancel. Build it once. (It's exactly the orchestration layer `WORKBENCH_PLAN` punted on by
  degrading maj@k-deep to a status-then-panel stub — that stub is the tell that chat is the wrong
  shape for fan-out.)
- **The natural environment is a notebook, not a chat and not a CI board.** Maths work is
  *non-linear, cumulative, literate, and generative* — you build a corpus of definitions, examples,
  lemmas, conjectures, results, and you revise it. A chat throws that away after each turn; a CI
  board is right for *one* burst but not for a living document. A computational notebook keeps the
  corpus, interleaves prose+LaTeX with computed results, re-runs, and models the
  generate→test→prove **dataflow** natively. The fleet board we want for "many attempts" is then
  **a cell's output widget**, embedded in the document — not the whole app.
- **Markdown + LaTeX is the only interchange format — in *and* out.** Paste a problem from a paper,
  arXiv, a Lean file, or an LLM chat; get back copy-pasteable markdown to drop into your paper. Zero
  lock-in, plugs into the entire maths ecosystem. The engine is *already* markdown-in / markdown-out
  (the Phase-B shim proves it: `_render_pieces` emits `\(…\)`/`\boxed{}`/fences). The studio just
  makes that ergonomic, non-linear, and persistent.
- **AI proposes, the cheap oracle disposes, the human curates.** "Algorithmic/AI generation of
  hypotheses" only pays off if generation is *abundant* and filtering is *cheap*. The Kernel
  (sympy/numpy — already built as the TIR executor) is also a **falsification oracle**: it kills a
  false conjecture numerically/symbolically in milliseconds. So discovery is the trust ladder
  applied to *generation*: generate a flock → auto-falsify in parallel (most die for ~free) →
  survivors earn the expensive solve/prove fan-out → verified results enter the corpus. Cheap to
  refute, expensive to prove — exploit the asymmetry.
- **Solver-first; it's usable *now*.** The prover (Lean, `PLAN.md` §5) is gated and unbuilt, so for
  the foreseeable future the studio *is* a solver. Everything here ships against the existing maj@k
  fan-out; the prover later **drops into the same job + widget** by swapping the reducer
  (cluster→verify) and the per-attempt engine (TIR/CoT→`prove_one`). Don't block on it.

## 1. What already exists (reuse; do not rebuild)

- **Engine:** `solver_loop` (TIR loop + `Kernel` executor + `extract_boxed`), `strategies`
  (async `cot`/`self_verify` + the thinking channel), `eval.solve_graded_async` (**maj@k** —
  k seeded chains, `asyncio.gather`, the vote), `providers` (the model×provider registry),
  `contenders.jsonl` (the lineup = the lanes of a fan-out).
- **Fan-out primitives:** for rented generalists, **`asyncio.gather` + a `Semaphore`** (as in
  `eval`/`audition`) is the whole fan-out — the provider rate limit is the ceiling. `fanout.py`
  (Modal `.map` over seeds, container/kernel per chain, scale-to-zero) is the **prover's** future
  substrate (sandbox-per-call), not the generalist path — repurpose it for Pass@k.
- **The envelope (the multiplexing seam):** `streaming.ev` — `reasoning_delta` · `thinking_delta`
  · `code` · `tool_result` · `final_answer{boxed, candidate_boxed, agreement, …}` · `error`, plus
  reserved prover events `proof`/`goal_state`/`verdict`. Each attempt **is** an envelope stream;
  a fleet is **N envelopes side by side**. Consumers dispatch on `type`.
- **The per-attempt renderer:** `shim._render_pieces` (FOIM buffer, fenced output, trust verdict,
  agreement panel, thinking channel, cost footer). One attempt-card renders with *this*. The shim's
  `_aiter_sync`, `stream_events`, picker, and `prices.json` are all reusable.
- **The async single call already exists** in embryo: `eval.solve_graded_async(problem, k, model,
  …) -> {pred, agreement, tokens, …}` — reuse its voting. **But it votes and *discards* the
  per-sample detail**; the answer-cluster widget needs the whole distribution, so the job layer's
  fan-out must **retain every attempt** (`solve_one_async` ×k, keeping each `{boxed, transcript,
  tokens, ttft, error}`) and cluster them — `solve_graded_async` is the reducer, not the collector.

## 2. Architecture decisions (locked)

1. **Library-first, zero frontend deps in core.** Public surface = a small `pudding` API
   (`solve`/`conjecture`/`falsify`/`prove`/`check`, `Job`, `get`) over the engine — pure async
   Python, importable and fully functional with no frontend installed. The notebook and shim import
   it; nothing imports *them*. Interactivity is **opt-in**: consume `job.stream()`, pass an optional
   `on_event=` sink, and bind UI controls to plain `Job` methods (`cancel`/`widen`) — none required
   to run.
2. **One job layer, three consumers** (async call · fleet widget · library). `Job` is the only
   async/fan-out abstraction, behind a `backend=` knob (default `"local"`).
   **Parallelism rule — match the primitive to the workload:** for *rented generalists* the per-call
   unit is just an HTTP call, so **local `asyncio.gather` + a `Semaphore` at the provider's rate
   limit IS the complete fan-out — Modal can't raise that ceiling** (more containers hit the same
   account limit). Modal (`backend="modal"`) re-enters only when a per-call owns a heavy/stateful
   thing: the prover's Lean sandbox (Pass@k), the `tools` kernel, or a self-hosted GPU with `n>1`
   shared prefill. So `fanout.py` is the prover's future, **not** the generalist path.
3. **Markdown + LaTeX is the interchange; results are frozen, citable artifacts.** Cells store
   markdown; problems enter as markdown; a result is a **canonical** markdown artifact (+ a
   structured sidecar), content-addressed and `pin`-able — so a stochastic run freezes with its
   provenance (seeds, models, cost) and re-renders reproducibly. Per-frontend `render(result,
   target=…)` adapters carry the delimiter dialect (`\(…\)` for OWUI, `$…$` for Quarto/pandoc); the
   core stays canonical. No proprietary maths format, no DSL.
4. **marimo is the reference *interactive* app — an optional consumer, not a dependency.** Shipped
   as a `pudding[studio]` extra; the core never imports it. It earns the slot by being markdown+LaTeX
   native, reactive (edit a definition → downstream tests invalidate), a Python-file notebook
   (library/app duality + git for free), and app-servable. Two frontends, two stages of one
   pipeline: **marimo = the lab** (live, stochastic, interactive); **Quarto = the publication**
   (consumes a *pinned/frozen* result, reproducibly). Each does the half it's good at.
5. **The Kernel is a falsification oracle, not just a TIR executor.** `falsify(conjecture)` runs a
   cheap numeric/symbolic counterexample search. Discovery = generate → falsify → solve/prove.
6. **Reuse the envelope + renderer + fanout.** The fleet widget is N envelope-streams multiplexed;
   each card is `_render_pieces`. New code is orchestration + view, not a new engine.
7. **Solver reducer now, prover reducer later** — same `Job`/widget. Solver: cluster by answer +
   within/cross-model agreement (trust in the *spread*). Prover: first/ranked **verified** (trust in
   the *cell*).
8. **Backend-agnostic, persistent jobs.** A `Job` survives the client process (a store), so an
   agent/cron/notebook-reload can submit, detach, and collect. Default backend is **local asyncio**
   (the right and complete primitive for rented generalists — see #2); Modal is opt-in for the
   sandbox/GPU-per-call regimes. Identical API either way.
9. **Library owns the view-model + a static render; the frontend owns the interactive shell.** The
   answer-cluster / flock / proof-gallery *data* (clusters, agreement, per-attempt rows, cost) and a
   static markdown/HTML rendering of it live in the library; live widgets + controls live in
   `studio/`. So even rendering pulls no frontend dep into the core.

## 3. The UX: "write maths, get maths back" (the environment)

**The studio** = a markdown-native reactive notebook. A cell is prose+LaTeX you write (or paste),
or an **operation** invoked on a selection/cell, whose output renders *inline* as a live widget and
resolves into a copy-pasteable markdown artifact that becomes part of the document — and can feed
the next operation.

**The verbs** (each returns a `Job`; each renders as a widget; each resolves to an artifact):

| verb | does | widget (the in-cell instrument) | trust |
|------|------|--------------------------------|-------|
| `solve(problem)` | maj@k / portfolio fan-out | **answer-cluster board**: clusters by boxed answer, within- *and* cross-model agreement, cost dial, kill switch, "add 5 more", drill-into-transcript | the *spread* |
| `conjecture(ctx, n)` | AI-generate n hypotheses from data/goal/corpus | **flock**: n candidates, provenance | unranked |
| `falsify(conj)` | cheap Kernel counterexample search | the flock **thinning** (50→6 survive) | refuted / survives |
| `prove(stmt)` *(gated)* | Lean fan-out (Pass@k) | **proof gallery**: ranked verified proofs (length·time·cost) | the *cell* (Lean ✓) |
| `check(claim)` | route to the right verifier | reuses the above | — |

**Why these widgets, carefully** (the no-verifier setting makes the solver surface an *epistemic
instrument*, not a pass/fail matrix):
- **The answer distribution is the product.** `144 ×7` lockstep vs `144 ×3 / 142 ×2 / 138 ×2`
  scattered tell you completely different things; the *shape of agreement* is the confidence signal,
  and the *dissent is diagnostic* (a near-miss = an arithmetic slip; a far-miss = a conceptual fork).
  This is the honest, fan-shaped version of Phase B's single `agreement m/k` fraction.
- **Cross-model consensus > single-model self-consistency.** Independent architectures agreeing
  (Qwen *and* DeepSeek *and* Kimi → 144) is the strongest trust available without an oracle. Only the
  fleet exposes it; it's why lanes should be a **heterogeneous portfolio** (`contenders.jsonl`).
- **Adaptive width is a buy-confidence dial.** With no oracle, more samples is the only way to
  tighten confidence (and it saturates). The widget shows diminishing returns live and lets you spend
  more compute on demand.
- **Disagreement triage puts the human in the loop as a feature.** When the fleet splits, **diff the
  divergent transcripts at the step they fork** and let the expert rule. "The solver hands you the
  contested step; you decide" — the collaborative mode a chat can't offer.

**Cadence.** Solver attempts are seconds-to-a-minute and cheap, so the studio runs them
*watch-live*; heavy bursts (wide portfolios, future proofs) run **detached** — submit, close the
tab, get notified, reopen to a persisted result. The notebook supports both because a cell binds to
a `Job` id, not a held connection.

**Headless & agentic — the studio is not required.** The same verbs drive a batch experiment, a
nightly conjecture-mining cron, or an autonomous agent that fans out maths and collects results:
`solve()` from a script, detach, collect by job id, never attach a UI. The studio is how a *human*
opts into watching/steering; the library is the backend either a human or a machine calls. A
headless caller just `await`s; an interactive one consumes `job.stream()` and binds buttons to
`job.cancel()`/`job.widen()` — the difference is entirely in the consumer, never the core.

**Prompt sources & the explore-a-space grid.** A problem can come from a paste, a benchmark slice
(`eval.load_data`), or a **parametric template** (`"Is {n}²+1 prime?"` over `n=2..20`) — the killer
case for exploration and conjecture-mining. Running `solve` over a *set* of `prompts × lineup`
yields a **grid** (cell = prompt × contender; drill-in = its k clustered samples) — the same
answer-cluster instrument, batched. That batch-explorer view is one more widget over the same job
layer, not a separate engine.

**The generative loop (AI doing maths, disciplined).** A `conjecture` cell takes context — a
selection, the notebook's accumulated corpus, or raw data/examples — and proposes hypotheses
(closed forms from a sequence; lemmas toward a goal; generalizations of a result). Every candidate
is **cheaply falsified by the Kernel** before it costs real compute, and **provenance-tracked**
(which model proposed it, which checks it survived). The widget shows the flock thinning; survivors
become "candidates worth proving" you fan out on. Division of labour: **AI proposes abundantly, the
oracle disposes ruthlessly, you curate the survivors.**

## 4. The library API (the async function call)

```python
import pudding

# submit returns immediately — a persistent, cancellable handle
job = pudding.solve("Find the remainder when 7^999 is divided by 1000.",
                    k=8, models=["deepseek-v4-pro", "qwen3.7-max"], budget="$0.50")

job.id                                   # survives this process (store-backed)
async for ev in job.stream():            # live multiplexed fleet events (per-attempt envelopes)
    ...
result = await job                       # or job.result(timeout=…) — blocks for the reduction
result.answer                            # "143"
result.agreement                         # {"143": 7, "142": 1}  ·  cross-model breakdown
result.markdown                          # copy-pasteable canonical artifact (LaTeX) for your paper

# interactivity is OPT-IN — nothing here imports a frontend
job = pudding.solve(problem, on_event=sink)   # optional progress sink: a UI, a logger, or nothing
job.widen(k=5); job.cancel(); job.budget = "$1"   # plain methods — a UI just binds buttons to them
later = pudding.get(job.id)              # reconnect from a cron / agent / reopened notebook

# lab → publication: freeze a stochastic run, re-render per frontend
frozen = pudding.pin(result)             # content-addressed + provenance-stamped → reproducible
render(frozen, target="quarto")          # delimiter-dialect adapter; "owui" / "plain" too

# sync convenience for scripts:
ans = pudding.solve("…").result().answer

# the generative loop:
flock   = pudding.conjecture(context=notebook.corpus, n=50)
alive   = pudding.falsify(flock)         # Kernel oracle, parallel → survivors
proven  = pudding.prove(alive)           # gated; same Job/widget
```

**Contracts:**
- `Job = {id, spec, attempts: [Attempt], status, result, cost}`; `Attempt =
  {lane: (model,rung,seed,budget), envelope-stream, outcome}`. `spec.reducer ∈
  {vote, cross_model_consensus, first_verified, ranked_verified}`. Controls — `cancel()`,
  `widen(k)`, `budget` — are plain methods (work headless; a UI just binds buttons to them).
- **Markdown artifact** = rendered canonical markdown+LaTeX **+** a structured sidecar (answer,
  distribution, per-attempt provenance, cost, verification status) in front-matter / a fenced
  metadata block — so results **round-trip**: paste into a paper, *or* feed back as `conjecture`
  context. Results are first-class documents; that's what closes the generative loop.
- **`Result` is content-addressed and `pin`-able.** `pudding.pin(result)` freezes a stochastic run
  with its provenance for reproducible re-render (the marimo-lab → Quarto-publication hop).
  `render(result, target=…)` adapters carry the per-frontend delimiter dialect; **interactive
  widgets live in `studio/`, never the library.**
- **Store** persists jobs (sqlite / a file / Modal Dict — see open questions) so async/detached use
  works across processes.

## 5. Build order (phases)

- **P1 — the library + job layer (solver-only). ✅ BUILT** (`pudding/`). `solve`/`get` + `Job`
  (`await` / `.result()` / `.stream()` / `.cancel()` / `.widen(k)`, store-backed) over local-asyncio
  fan-out (`solve_one_async` ×k collector + clustering by `eval._norm_latex`, within/cross-model
  agreement — the collector keeps every sample). `pin`/`get_pin` (content-addressed frozen artifacts),
  `view_model`/`to_html` (decision #9 — the library's static view; widgets stay in `studio/`),
  `render(target=)`. Headless, zero frontend deps; opt-in `on_event`. `backend="modal"` deferred to
  the prover. 11 headless tests; live cross-model smoke (4/4 → 144). *The async function call your
  code can invoke — delivered.*
- **P2 — the notebook app (the studio). 🚧 SCAFFOLDED** (`studio/app.py`, `pudding[studio]` extra).
  marimo reference app: paste a problem, model multiselect (from the lineup) + k slider, a Solve
  run-button → `pudding.solve` → the inline **answer-cluster board** (cluster table + cross-model
  flag + footer) and per-attempt transcript drill-in, over `view_model` (thin shell, decision #9).
  Builds headless; **interactive run is the user's to drive** (`uv run --extra studio marimo run
  studio/app.py`). **Done since:** live **stream-fill** (`job.stream()`), a real **kill-switch**
  (marimo interrupt / Solve-re-press → `job.cancel()`, library cancels children), **CoT in the
  drill-in** (#3), honest **error** rendering + a `timeout=` cap (#4), leaner default (deepseek, k=2).
  **Remaining:** the add-more (`widen`) / cost-dial controls as buttons, and copy-out/`pin` + cell↔job
  binding → folded into P5 (#1). **DoD:** write/paste a problem, run it, watch the fleet resolve, copy out.
- **P3 — the generative loop. ✅ BUILT** (`pudding/discovery.py`). `conjecture` (an LLM proposes n
  falsifiable claims from a context, each shipping a Python `counterexample()` harness, provenance-
  tagged) + `falsify` (each harness runs in an **isolated subprocess oracle** — genuinely parallel,
  real timeout/kill, crash-isolated, sympy/numpy from the venv — → `refuted`+witness / `survives` /
  `error`) + `discover` (the chain). Two honesty rails: **the oracle decides, not the prose**, and
  **surviving ≠ proven** (a survivor is *not refuted by the search* = a candidate worth proving). The
  flock markdown + `flock_view_model` are the library's static view (decision #9); the studio **✨
  Discover** section shows the flock thinning live then routes a chosen survivor into the `solve`
  fan-out (`"Prove or disprove: …"`). Tolerant JSON parse with **per-object salvage** (a truncated
  reasoning-model array still yields its complete conjectures). 9 offline tests (oracle run for real);
  live: deepseek proposed 4 NT claims → oracle refuted 3 (n²+n+41 @40, Σprimes @3, p²−1∣48 @5) and
  survived 1 (n⁴+4ⁿ composite). **DoD met:** from a context, generate hypotheses, auto-cull the false
  ones cheaply, surface candidates worth proving.
- **P4 — the prover drop-in (gated on `PLAN.md` §5/Phase C prover).** `prove` → Lean Pass@k fan-out,
  the **proof-gallery** reducer, same job/widget. **DoD:** a verified-proof gallery for a statement.

## 6. Open questions (resolve while building)

- **Notebook tech — RESOLVED:** marimo = the reference *interactive* app (a `pudding[studio]` extra,
  not a core dep — Quarto isn't a natural home for non-deterministic output); Quarto = a first-class
  consumer of *pinned/frozen* results for publishing; Jupyter a possible third consumer. The library
  stays frontend-neutral, so this is revisitable without touching the engine.
- **Job store:** sqlite (queryable, one file) vs a results dir (git-friendly, matches the audition's
  `results/*.jsonl`) vs Modal Dict (cloud-native for detached bursts). Probably sqlite local +
  Modal-backed for cloud jobs.
- **Artifact sidecar:** how much structure to standardize so results round-trip *and* re-render
  *and* feed back as context, without inventing a format people resent.
- **`conjecture` context:** selection vs whole-notebook corpus vs uploaded data — and how to keep the
  prompt honest (it must propose, not assert; the oracle, not the prose, decides).
- **Dedup/cluster** of equivalent answers/conjectures: cluster by `eval._norm_latex` + int/decimal
  (cheap, live) rather than pairwise `math_verify` (correct but too expensive to run across a live
  grid) — accept that symbolic-equal-but-different-form answers may split into separate clusters;
  `None`/`""` → the `∅` (no-answer) cluster.
- **Cost-budget semantics:** hard cap (kill at $X) vs warn-and-continue; auto-stop-on-first-verified
  default; pre-flight estimate from `prices.json`.
- **Does the Phase-B chat shim survive** as the "quick single question" lightweight client, or is it
  subsumed by a one-cell studio? (Lean: keep it; it's a thin library consumer and a good smoke test.)
- **Run persistence management (P5 follow-up, deferred):** the job/pin store grows unbounded and
  `recent()` is a full-dir scan. Later we'll want **indexing, sorting, filtering, and deleting** runs
  (and pins) — a proper browser over an index (ties to the sqlite-vs-dir store question above).

## 7. File map & non-goals

- **New** `pudding/` (or top-level): `jobs.py` (Job/Attempt/store + the multiplex), `api.py` (the
  verbs), `artifacts.py` (canonical markdown ⇄ sidecar + `pin`/`render` adapters), `discovery.py`
  (the generative loop: `conjecture`/`falsify`/`discover` + the subprocess oracle + flock view),
  `store.py` — **zero frontend deps.** `studio/` — the marimo app + live widgets, installed via
  `pudding[studio]`, importing `pudding` (never the reverse).
- **Reuse unchanged:** `solver_loop`, `strategies`, `eval`, `providers`, `contenders.jsonl`,
  `streaming`, `fanout`, `shim._render_pieces`/`prices.json`.
- **Non-goals (Phase C):** the Lean prover itself (gated, Phase D); multi-user/hosted SaaS &
  real-time collab (single-user local first); a bespoke web frontend (use marimo); a proprietary
  maths DSL (markdown+LaTeX only); the `tools` rung (still gated).

## 8. Definition of done (Phase C, solver)

`import pudding` gives a working **async `solve()`** your code can fire, detach from, and collect
(persistent jobs, cloud fan-out, markdown results) — **with no frontend installed.** The **studio**
notebook (an opt-in `pudding[studio]` consumer) lets you write/paste
maths, fan out many attempts on the cloud, watch the answer-distribution instrument resolve *inside
your document*, copy the result into a paper, and run a generate→falsify→solve discovery loop. The
prover, when it exists, is a reducer swap away on the identical surface. Library and app, one engine.

## 9. Sketched next scopes (P5 = #1 reuse, P6 = #2 batch→grid)

### P5 — id-addressed reuse (the unit is the artifact, not the cell) ✅ BUILT
*Done: `api.recent`; studio ♻ Reuse section — copy-out (id + artifact), recent dropdown, load-by-id, 📌 pin. Concurrency default 8→16.*
**Problem.** A marimo cell is ephemeral (recomputed on re-run, gone at session end); the unit of
maths — a `Result` — must outlive it. Reuse-by-cut-and-paste is clunky and lossy. **The durable unit
is the id-addressed artifact** (a `job id`, or a content-addressed `pin id`); cells are just lenses.

**Build (all studio + thin library, no engine change):**
1. **Copy-out (cheap, do first).** A one-click "copy" on a result — render `render(result)` in a
   fenced block + a `mo.ui` copy button. Makes the human path (into a paper) frictionless.
2. **Recent-results browser.** Library: `recent(n) -> [{id, problem, answer, agreement, created}]`
   (project summaries from the job store; the store already has `list_ids()`). Studio: a dropdown of
   recent runs → load → render its board. So "use last session's output" = pick it, not re-run it.
3. **Load-by-id.** A text input → `pudding.get(id)` / `get_pin(id)` → board. Pair with the id shown
   prominently on every result (so it's copyable across sessions/machines).
4. **Feed-forward (the generative seam).** Insert a pinned artifact as *context* for the next
   `solve`/`conjecture` (e.g. "given this verified lemma: …"). This is where #1 meets P3.

**Contracts:** `store.summaries()` (or `recent`) reading the job dir; `api.recent(n)`; studio cells
for copy / recent-dropdown / load-by-id. **Decision:** results are addressed by id; the markdown
artifact is the interchange; cut-and-paste stays as the human fallback, never the only path.

### P6 — batch → grid (parallel, one view, one rate budget) ✅ BUILT
*Done: `api.solve_many` (one shared Semaphore = global rate budget) returns handles immediately;
`Job.summary()`/`completed` for poll-based feedback; studio ▦ Batch section = **launch-don't-await**
+ a `mo.ui.refresh` timer polling a live grid + ■ stop-all. Live-verified: launch non-blocking,
grid fills as background jobs complete. Async feedback without blocking the launching cell.*
**Problem.** Launching many loops at once is valuable (explore a *space*), but N live boards in
marimo is unreadable and unmaintainable (the reactive model fights dynamic-N UI; N lifecycles to
manage). **Do it as one batch op → one grid**, parallelism in the library.

**Build:**
1. **Global rate budget (load-bearing, do first).** Today each `Job` has its own `Semaphore(8)`; N
   jobs × k would blow the provider rate limit. Thread a **shared** semaphore into `_collect`
   (`sem=`) so total in-flight provider calls ≤ one cap across the whole batch.
2. **`solve_many(problems, *, models, k, timeout, concurrency)`** → a set of `Job`s under the shared
   cap → a grid view-model (`to_grid`): row = problem (× model optional), cols = voted · agreement ·
   $ · time; drill-in = that cell's full board. Prompt sources: a pasted list, a benchmark slice
   (`eval.load_data`), or a **parametric template** (`"Is {n}²+1 prime?"` over `n=2..20` — the
   exploration killer-case). Persist the set as a *sweep* id (reuses P5's store).
3. **Studio grid.** One cell: source control + run → `mo.ui.table(to_grid(...))`, row-select →
   drill-in board. Not N boards.

**Contracts:** `_collect(sem=)`; `api.solve_many(...) -> [Job]` + `artifacts.to_grid(results)`;
studio table+drill-in. **Maintainability rule:** parallelism lives in the library (one batch op +
one shared rate cap); marimo renders one grid. Modal stays out (rented generalists are rate-limit
bound — local asyncio is the ceiling; §2).

Sequencing: P5 first (it also closes P2's copy-out/binding remainder and feeds P3); P6 next.
