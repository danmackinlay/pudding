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


if __name__ == "__main__":
    app.run()
