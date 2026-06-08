# FORK.md — the two-track split (read this first)

As of commit `6193fbf` (the audit taut-up; 66 tests green, tree clean) the work forks into **two
tracks in two worktrees**, sharing one frozen core. This file is the map and the discipline. Both
track plans (`SOLVER_UX_PLAN.md`, `PROVER_PLAN.md`) assume you've read it.

## The principle: fork the frontier, not the core
Both tracks are *consumers* of the same `pudding` library. The split is about divergent **frontiers**
(solver UX vs. prover/formalisation), not divergent cores. What must **not** diverge is the contract:

- **The library API** — `pudding.solve/solve_many/conjecture/falsify/discover/get/recent/pin/get_pin`,
  and the records `Job` / `Result` / `Attempt` / `Cluster` / `Flock` / `Conjecture`.
- **The streaming envelope** — `streaming.ev(type, **kw)` (the `attempt`/`done`/`conjecture`/`verdict`
  events; reserved prover events `proof`/`goal_state`/`verdict`).
- **The view-model + render seam** — `artifacts.view_model` / `to_html` / `render(obj, target=)`,
  `discovery.flock_view_model` (decision #9: library owns the data + static render; widgets live in
  frontends).
- **The store** — `store.read/write/read_pin/write_pin/list_ids` (now atomic + corrupt-tolerant).
- **The trust-ladder philosophy** — `FINDINGS.md`: maj@k agreement is the confidence surface; the
  prover is "trust the cell," the solver is "trust the spread." Surface trust, never fake it.

## The tracks
| | **Track 1 — Solver UX** | **Track 2 — Prover + formalisation** |
|---|---|---|
| worktree | **this one** (`claude/phase-b-workbench`, the base / new-main) | fresh: `../pudding-prover` (`claude/prover`) |
| plan | `SOLVER_UX_PLAN.md` | `PROVER_PLAN.md` |
| risk | low — additive UX over a stable library | high — Lean, Modal, Novita, the faithfulness gate; gated (PLAN §5) |
| owns | `studio/`, store→index work | new `pudding/prove.py` + `formalize.py`, `prover_loop.py`, `verifier/`, `fanout.py` |
| reuses (read-only) | the whole library | the library + `discovery.py`'s subprocess oracle |

## The discipline (so the eventual merge is painless)
1. **Treat the contract above as frozen.** Don't change `jobs.py`/`api.py`/`streaming.py`/`store.py`
   signatures inside a fork on a whim.
2. **A genuinely-needed core change lands on the base first** (a small commit on `claude/phase-b-workbench`),
   then both worktrees rebase/merge it forward. The likely case: the prover track wants to *extend*
   the falsify oracle (a "compare a formal statement's concrete instances against the informal
   harness" check) — that extension is a core change → base first.
3. **Each fork adds its own modules**; they should almost never both edit the same file. Solver-UX
   ≈ `studio/` + `store.py` index; prover ≈ new modules + the parked `verifier/`/`prover_loop.py`.
4. **The prover is a reducer swap on the identical `Job`/widget surface** (STUDIO_PLAN decision #7) —
   if you find yourself rebuilding the job layer, stop; you're diverging the core.

## Live-vs-parked modules (also in README)
Phase-C engine in use: `solver_loop`, `strategies`, `eval`, `providers`, `streaming`, the `pudding/`
package, `studio/`. Standalone CLIs: `audition.py`, `serve.py`. Parked for the gated prover (Phase D):
`fanout.py` (Modal Pass@k), `prover_loop.py`, `verifier/`. `shim.py` is the alternate Open-WebUI
frontend (a thin library consumer). Prover research is frozen in `PROVER_RESEARCH_ADDENDUM.md`.

## Git mechanics
```
# base is committed at the docs commit on claude/phase-b-workbench (this worktree continues here).
git worktree add ../pudding-prover -b claude/prover            # off current HEAD (the base)
# Solver UX continues in this worktree (optionally on a claude/solver-ux branch off base).
```
Worktrees share one object store; you cannot check out the same branch in two worktrees. To land a
shared-core fix from a fork: commit it, then in the base worktree `git merge --ff-only` (or cherry-pick),
and the other worktree pulls it forward.
