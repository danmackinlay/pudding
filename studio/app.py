"""pudding studio — the marimo notebook surface (STUDIO_PLAN Phase C, P2).

Write maths, get maths back: paste a problem (markdown + LaTeX), pick models and k, hit Solve;
many attempts fan out on the backend and the **answer-cluster board** fills in attempt-by-attempt.
A thin reactive shell — all fan-out / clustering / artifacts live in the headless `pudding`
library (decision #9); this file only wires controls to it and renders the view-model.

    uv run --extra studio marimo edit studio/app.py      # author / explore (the lab)
    uv run --extra studio marimo run  studio/app.py      # serve as an app

Lifecycle: the runner streams `job.stream()` so progress shows live; a `finally: job.cancel()`
means marimo's interrupt (■) **and** re-pressing Solve both kill the in-flight fan-out and
supersede cleanly (the kill closes the HTTP — see pudding.jobs._collect).

Marimo is an OPTIONAL consumer (`pudding[studio]`): it imports pudding, never the reverse.
"""

import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _():
    import asyncio
    import os
    import sys

    import marimo as mo

    # marimo puts the notebook's dir (studio/) on sys.path, not the repo root — so bootstrap the
    # root (the dir containing the `pudding` package) by walking up from cwd / the notebook dir.
    for _b in (os.getcwd(), *sys.path[:1]):
        _p = os.path.abspath(_b)
        while _p != os.path.dirname(_p):
            if os.path.isfile(os.path.join(_p, "pudding", "__init__.py")):
                if _p not in sys.path:
                    sys.path.insert(0, _p)
                break
            _p = os.path.dirname(_p)

    import pudding
    from pudding import lineup

    return asyncio, lineup, mo, pudding


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # pudding studio
    *write maths, get maths back* — fan out many attempts, read the spread, keep the working.
    """)
    return


@app.cell(hide_code=True)
def _(mo, pudding):
    def board(*, progress=None, total=None, result=None, job_id=None):
        """The answer-cluster board: a live progress list while attempts land, then the voted
        answer + an id line + copy-out + cluster table + per-attempt transcripts. Pure render."""
        if result is not None:
            vm = pudding.view_model(result)
            md = pudding.render(result)
            blocks = [mo.md(md)]                          # headline · agreement · footer (or error)
            ids = " · ".join(s for s in [f"job `{job_id}`" if job_id else "",
                                         f"pin `{vm['pin']}`" if vm.get("pin") else ""] if s)
            if ids:
                blocks.append(mo.md(f"<small>{ids}</small>"))
            blocks.append(mo.accordion({"📋 copy artifact (markdown)": mo.md(f"```\n{md}\n```")}))
            if len(vm["clusters"]) > 1:
                blocks.append(mo.ui.table(
                    [{"answer": c["answer"], "votes": c["count"], "models": ", ".join(c["models"])}
                     for c in vm["clusters"]], selection=None, label="answer clusters"))
            blocks += [mo.md("#### the working"), mo.accordion({
                f"{a['model']} #{a['seed']} → {a['boxed'] if a['boxed'] is not None else '∅'}":
                    mo.md(((a["thinking"] + "\n\n---\n") if a.get("thinking") else "")
                          + (a["transcript"] or a["error"] or "*(empty)*"))
                for a in vm["attempts"]})]
            return mo.vstack(blocks)
        prog = progress or []
        rows = "\n".join(
            f"- `{e['lane']}` #{e['seed']} → "
            + (f"**{e['boxed']}**" if e.get("boxed") is not None
               else (f"_{e['error']}_" if e.get("error") else "∅"))
            for e in prog) or "*starting…*"
        return mo.vstack([mo.md(f"running… **{len(prog)}/{total}** samples"), mo.md(rows)])

    return (board,)


@app.cell
def _(lineup, mo):
    known = sorted(lineup._MAP) or ["deepseek-v4-pro"]
    default = ["deepseek-v4-pro"] if "deepseek-v4-pro" in known else known[:1]
    example = "Find the remainder when 7^999 is divided by 1000."  # template from samples.jsonl (answer 143)
    problem = mo.ui.text_area(value=example, placeholder="Paste a problem (markdown + LaTeX)…",
                              rows=4, full_width=True)
    models = mo.ui.multiselect(options=known, value=default, label="models")
    k = mo.ui.slider(1, 12, value=2, label="k (samples / model)")
    run = mo.ui.run_button(label="Solve")
    mo.vstack([problem, mo.hstack([models, k], justify="start", gap=2), run])
    return k, models, problem, run


@app.cell(hide_code=True)
async def _(asyncio, board, k, mo, models, problem, pudding, run):
    mo.stop(not run.value,
            mo.md("◦ press **Solve** to fan out — interrupt (■) or press Solve again to restart."))
    mo.stop(not problem.value.strip(), mo.md("◦ enter a problem first."))
    total = len(models.value) * k.value
    attempts = []
    job = pudding.solve(problem.value, k=k.value, models=list(models.value), timeout=60)
    try:
        async for ev in job.stream():
            if ev.get("type") == "attempt":
                attempts.append(ev)
                mo.output.replace(board(progress=attempts, total=total))
        result = await job
        mo.output.replace(board(result=result, job_id=job.id))
    except asyncio.CancelledError:
        mo.output.replace(mo.md(f"⏹ **stopped** — {len(attempts)}/{total} samples."))
        raise
    finally:
        if job.status not in ("done", "error"):
            job.cancel()     # marimo interrupt / Solve re-press → kill the in-flight fan-out
    return


@app.cell(hide_code=True)
def _(mo):
    refresh = mo.ui.run_button(label="↻ refresh")
    return (refresh,)


@app.cell(hide_code=True)
def _(mo, pudding, refresh):
    _ = refresh.value                                  # re-read the list when refresh is clicked
    runs = pudding.recent(20)                          # newest runs from the job store (any session)
    options = {f"{(s['answer'] or '∅')} · {s['problem']}  [{s['id']}]": s["id"] for s in runs}
    pick = mo.ui.dropdown(options=options or {"(no past runs yet)": ""}, label="recent runs")
    by_id = mo.ui.text(placeholder="…or paste a job / pin id", label="load by id")
    mo.vstack([mo.md("### ♻ Reuse a past run — the durable unit is the id, not the cell"),
               mo.hstack([pick, refresh], justify="start", gap=2), by_id])
    return by_id, pick


@app.cell(hide_code=True)
def _(board, by_id, mo, pick, pudding):
    rid = (by_id.value or "").strip() or pick.value
    mo.stop(not rid, mo.md("◦ pick a recent run or paste an id to reuse it."))
    loaded_job = pudding.get(rid)                      # a Job…
    reused = loaded_job._result if (loaded_job and loaded_job._result) else pudding.get_pin(rid)  # …or a pin
    mo.stop(reused is None, mo.md(f"◦ no run found for `{rid}`."))
    board(result=reused, job_id=rid)                   # the loaded board (copy-out + id inside)
    return (reused,)


@app.cell(hide_code=True)
def _(mo, reused):
    _ = reused                                         # only show pin once a result is loaded
    pin_btn = mo.ui.run_button(label="📌 pin → citable id")
    pin_btn
    return (pin_btn,)


@app.cell(hide_code=True)
def _(mo, pin_btn, pudding, reused):
    mo.stop(not pin_btn.value, mo.md(""))
    pinned = pudding.pin(reused)
    mo.md(f"📌 pinned as `{pinned.pin}` — frozen + reproducible; reload with this id.")
    return


@app.cell
def _(lineup, mo):
    bproblems = mo.ui.text_area(
        value=("Find the remainder when 7^999 is divided by 1000.\n"
               "What is 12*12?\n"
               "How many positive integers less than 1000 are divisible by neither 5 nor 7?"),
        rows=4, full_width=True, label="problems (one per line)")
    bmodels = mo.ui.multiselect(options=sorted(lineup._MAP) or ["deepseek-v4-pro"],
                                value=["deepseek-v4-pro"], label="models")
    bk = mo.ui.slider(1, 8, value=2, label="k / problem")
    run_batch = mo.ui.run_button(label="Run batch")
    stop_batch = mo.ui.run_button(label="■ stop all")
    mo.vstack([mo.md("### ▦ Batch — explore a space (parallel, one shared rate budget)"),
               bproblems, mo.hstack([bmodels, bk], justify="start", gap=2),
               mo.hstack([run_batch, stop_batch], justify="start", gap=2)])
    return bk, bmodels, bproblems, run_batch, stop_batch


@app.cell
def _(bk, bmodels, bproblems, mo, pudding, run_batch):
    # launch-DON'T-await: solve_many returns handles immediately (jobs run on marimo's loop);
    # the grid below polls them, so this cell never blocks (STUDIO_PLAN P6).
    mo.stop(not run_batch.value, mo.md("◦ enter problems (one per line), then **Run batch**."))
    problems = [p.strip() for p in bproblems.value.splitlines() if p.strip()]
    mo.stop(not problems, mo.md("◦ no problems entered."))
    batch = pudding.solve_many(problems, k=bk.value, models=list(bmodels.value), timeout=60)
    mo.md(f"launched **{len(batch)}** problems — the launch did **not** block; live grid below ↓")
    return (batch,)


@app.cell
def _(batch, mo):
    _ = batch
    refresh_grid = mo.ui.refresh(default_interval="1s")     # poll the background jobs each second
    refresh_grid
    return (refresh_grid,)


@app.cell
def _(batch, mo, refresh_grid):
    refresh_grid.value                                      # tick → re-poll live state
    rows = [j.summary() for j in batch]
    done = sum(1 for r in rows if r["status"] in ("done", "error", "cancelled"))
    mo.ui.table(rows, selection=None,
                label=f"batch — {done}/{len(rows)} complete · drill in by pasting a row's id "
                      f"into the ♻ Reuse box above")
    return


@app.cell
def _(batch, mo, stop_batch):
    mo.stop(not stop_batch.value, mo.md(""))
    for _j in batch:
        _j.cancel()                                         # real kill — closes in-flight HTTP
    mo.md(f"■ stopped {len(batch)} jobs.")
    return


@app.cell
def _(lineup, mo):
    dcontext = mo.ui.text_area(
        value=("Elementary number theory over the integers. Look for closed forms, divisibility "
               "patterns, and primality claims — propose precise, testable statements."),
        rows=3, full_width=True, label="context (a selection, a corpus, or raw data)")
    dmodels = mo.ui.multiselect(options=sorted(lineup._MAP) or ["deepseek-v4-pro"],
                                value=["deepseek-v4-pro"], label="models")
    dn = mo.ui.slider(2, 12, value=6, label="n (conjectures)")
    gen_btn = mo.ui.run_button(label="✨ Discover (conjecture → falsify)")
    mo.vstack([mo.md("### ✨ Discover — AI proposes, the cheap oracle disposes, you curate"),
               dcontext, mo.hstack([dmodels, dn], justify="start", gap=2), gen_btn])
    return dcontext, dmodels, dn, gen_btn


@app.cell(hide_code=True)
def _(mo, pudding):
    def flock_board(flock=None, *, proposing=None):
        """The flock: live 'proposing…' then the thinned table + survivors + harness drill-in.
        The oracle's verdict (not the prose) decides; **surviving ≠ proven**. Pure render."""
        if flock is None:
            rows = proposing or []
            body = "\n".join(f"- `{r['id']}` ({r['origin']}) {r['statement']}" for r in rows)
            return mo.vstack([mo.md(f"proposing… **{len(rows)}** conjectures"),
                              mo.md(body or "*proposing…*")])
        vm = pudding.flock_view_model(flock)
        blocks = [mo.md(vm["markdown"]),
                  mo.accordion({"📋 copy flock (markdown)": mo.md(f"```\n{vm['markdown']}\n```")}),
                  mo.ui.table([{"id": c["id"], "status": c["badge"], "conjecture": c["statement"],
                                "witness / why": c["witness"] or c["detail"], "by": c["origin"]}
                               for c in vm["conjectures"]], selection=None, label="the flock")]
        blocks += [mo.md("#### the harnesses — the oracle runs these; the prose doesn't decide"),
                   mo.accordion({
                       f"{c['id']} {c['badge']} — {c['statement'][:60]}":
                           mo.md((f"*{c['rationale']}*\n\n" if c["rationale"] else "")
                                 + f"```python\n{c['check'] or '(no harness)'}\n```")
                       for c in vm["conjectures"]})]
        return mo.vstack(blocks)

    return (flock_board,)


@app.cell
async def _(dcontext, dmodels, dn, flock_board, gen_btn, mo, pudding):
    mo.stop(not gen_btn.value, mo.md("◦ set a context and press **Discover** — generate "
            "falsifiable conjectures, then cull the false ones for ~free before spending solve."))
    mo.stop(not dcontext.value.strip(), mo.md("◦ enter a context first."))
    _state = {"proposed": [], "verdicts": {}}

    def _sink(e):                                       # opt-in liveness → show the flock thinning
        if e.get("type") == "conjecture":
            _state["proposed"].append({"id": e["id"], "origin": e["origin"],
                                       "statement": e["statement"]})
            mo.output.replace(flock_board(proposing=_state["proposed"]))
        elif e.get("type") == "verdict":
            _state["verdicts"][e["id"]] = e["status"]
            _surv = sum(1 for s in _state["verdicts"].values() if s == "survives")
            _ref = sum(1 for s in _state["verdicts"].values() if s == "refuted")
            mo.output.replace(mo.md(f"falsifying… **{len(_state['verdicts'])}/"
                                    f"{len(_state['proposed'])}** checked · {_surv} survive · "
                                    f"{_ref} refuted"))

    flock = await pudding.discover(dcontext.value, n=dn.value, models=list(dmodels.value),
                                   timeout=8, on_event=_sink)
    mo.output.replace(flock_board(flock))
    return (flock,)


@app.cell
def _(flock, mo):
    survs = flock.survivors
    prove_pick = mo.ui.dropdown(
        options={f"{s.id}: {s.statement[:70]}": s.id for s in survs} or {"(no survivors)": ""},
        label="a survivor to put to the solver")
    prove_btn = mo.ui.run_button(label="⊢ Prove or disprove (fan out the solver)")
    mo.vstack([mo.md("#### survivors → the expensive fan-out  ·  *survived ≠ proven*"),
               mo.hstack([prove_pick, prove_btn], justify="start", gap=2)])
    return prove_btn, prove_pick


@app.cell
async def _(asyncio, board, flock, mo, prove_btn, prove_pick, pudding):
    mo.stop(not prove_btn.value, mo.md("◦ pick a survivor, then **Prove or disprove**."))
    sel = next((s for s in flock.survivors if s.id == prove_pick.value), None)
    mo.stop(sel is None, mo.md("◦ no survivor selected."))
    pjob = pudding.solve(f"Prove or disprove, with rigorous justification: {sel.statement}",
                         k=2, models=flock.models, timeout=90)
    pattempts = []
    try:
        async for pev in pjob.stream():
            if pev.get("type") == "attempt":
                pattempts.append(pev)
                mo.output.replace(board(progress=pattempts, total=pjob.total))
        mo.output.replace(board(result=await pjob, job_id=pjob.id))
    except asyncio.CancelledError:
        mo.output.replace(mo.md(f"⏹ **stopped** — {len(pattempts)}/{pjob.total} samples."))
        raise
    finally:
        if pjob.status not in ("done", "error"):
            pjob.cancel()
    return


if __name__ == "__main__":
    app.run()
