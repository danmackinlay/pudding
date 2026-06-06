"""pudding studio — the marimo notebook surface (STUDIO_PLAN Phase C, P2).

Write maths, get maths back: paste a problem (markdown + LaTeX), pick models and k, hit Solve;
many attempts fan out on the backend and the **answer-cluster board** resolves inline. A thin
reactive shell — all fan-out / clustering / artifacts live in the headless `pudding` library
(decision #9); this file only wires controls to it and renders the view-model.

    uv run --extra studio marimo edit studio/app.py      # author / explore (the lab)
    uv run --extra studio marimo run  studio/app.py      # serve as an app

Marimo is an OPTIONAL consumer (`pudding[studio]`): it imports pudding, never the reverse.
"""
import marimo

app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    import pudding
    from pudding import lineup
    return lineup, mo, pudding


@app.cell
def _(mo):
    mo.md(
        """
        # pudding studio
        *write maths, get maths back* — fan out many attempts, read the spread, keep the working.
        """
    )
    return


@app.cell
def _(lineup, mo):
    known = sorted(lineup._MAP) or ["deepseek-v4-pro"]
    default = [m for m in ("deepseek-v4-pro", "qwen3-7-max") if m in known] or known[:1]
    problem = mo.ui.text_area(placeholder="Paste a problem (markdown + LaTeX)…",
                              rows=4, full_width=True)
    models = mo.ui.multiselect(options=known, value=default, label="models")
    k = mo.ui.slider(1, 12, value=5, label="k (samples / model)")
    run = mo.ui.run_button(label="Solve")
    mo.vstack([problem, mo.hstack([models, k], justify="start", gap=2), run])
    return k, models, problem, run


@app.cell
async def _(k, mo, models, problem, pudding, run):
    # The run button gates the expensive fan-out so editing controls doesn't re-solve.
    mo.stop(not run.value, mo.md("*paste a problem, choose models + k, then press **Solve***"))
    mo.stop(not problem.value.strip(), mo.md("*enter a problem first*"))
    with mo.status.spinner(title=f"running {len(models.value)} × {k.value} samples…"):
        job = pudding.solve(problem.value, k=k.value, models=list(models.value))
        result = await job          # marimo supports top-level await
    return (result,)


@app.cell
def _(mo, pudding, result):
    vm = pudding.view_model(result)
    if vm["answer"] is None:
        head = "### No answer — the samples produced no boxed result."
    else:
        head = (f"### \\({vm['answer']}\\)  ·  agreement {vm['agreement']}"
                + (" · **cross-model ✓**" if vm["cross_model"] else ""))
    cost = f" · ~${vm['cost']:.4f}" if vm["cost"] is not None else ""
    footer = mo.md(f"<small>— maj@{vm['k']} · {', '.join(vm['models'])} · {vm['tokens']} tok{cost}</small>")
    clusters = mo.ui.table(
        [{"answer": c["answer"], "votes": c["count"], "models": ", ".join(c["models"])}
         for c in vm["clusters"]],
        selection=None, label="answer clusters",
    )
    transcripts = mo.accordion({
        f"{a['model']} #{a['seed']} → {a['boxed'] if a['boxed'] is not None else '∅'}":
            mo.md(a["transcript"] or a["error"] or "*(empty)*")
        for a in vm["attempts"]
    })
    mo.vstack([mo.md(head), clusters, footer, mo.md("#### the working"), transcripts])
    return


if __name__ == "__main__":
    app.run()
