"""pudding · prover studio — a glass-box demo (Track 2, PROVER_PLAN §1½).

Paste a Lean `theorem … := by sorry`, pick a tool, and watch the machine either Pass@k a whole
proof or DECOMPOSE it (draft-sketch-prove): a generalist drafts a flat lemma decomposition, the
Kimina compiler validates the *plan* before any leaf is proved, the leaf-prover closes each hole,
and the reassembled proof is re-verified end-to-end. Every step is shown — never a laundered ✓.

    direnv exec . uv run --extra studio marimo run  studio/prover_demo.py    # play
    direnv exec . uv run --extra studio marimo edit studio/prover_demo.py    # author

Needs (already set up here): Modal authed + the `pudding-verifier` app deployed
(`modal deploy verifier/modal_verifier.py`), and OPENROUTER_API_KEY in the env (direnv). Each run
is a real Opus + Modal call — roughly 30–70s and a few cents (Kimi/DeepSeek are cheaper).
"""
import marimo

__generated_with = "0.23.9"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _():
    import os
    import sys

    import marimo as mo

    # marimo only puts studio/ on sys.path — bootstrap the repo root (the dir with the `pudding`
    # package) by walking up, so `import pudding` resolves. (Same trick as studio/app.py.)
    for _b in (os.getcwd(), *sys.path[:1]):
        _p = os.path.abspath(_b)
        while _p != os.path.dirname(_p):
            if os.path.isfile(os.path.join(_p, "pudding", "__init__.py")):
                if _p not in sys.path:
                    sys.path.insert(0, _p)
                break
            _p = os.path.dirname(_p)

    from pudding.prove import prove
    from pudding.sketch import prove_by_sketch
    return mo, prove, prove_by_sketch


@app.cell(hide_code=True)
def _(mo):
    mo.md(
        """
        # pudding · prover studio  🔍
        *write a theorem, get a **verified, auditable** proof — or an honest "no".*

        Two tools, one unfakeable Lean compiler:

        - **draft-sketch-prove** — decompose into helper lemmas, let the compiler validate the
          *plan* (the combine step) **before** proving any part, close each leaf, reassemble, and
          re-verify the whole thing. The glass box.
        - **prove** — Pass@k a whole proof; the shortest candidate that compiles wins.

        A `sorry` never reads as ✓, and the model can't quietly weaken your statement.
        """
    )
    return


@app.cell
def _(mo):
    PRESETS = {
        "div6 · 6 ∣ n³−n  (decomposes into 2∣ and 3∣)":
            "import Mathlib\n\ntheorem div6 (n : ℤ) : (6 : ℤ) ∣ n ^ 3 - n := by sorry",
        "Gauss · 2·Σ i = n(n+1)":
            "import Mathlib\n\ntheorem gauss (n : ℕ) : 2 * ∑ i ∈ Finset.range (n + 1), i = n * (n + 1) := by sorry",
        "algebra · (a+b)² = a²+2ab+b²":
            "import Mathlib\n\ntheorem sq (a b : ℝ) : (a + b) ^ 2 = a ^ 2 + 2 * a * b + b ^ 2 := by sorry",
        "sum of odds · Σ(2i+1) = n²":
            "import Mathlib\n\ntheorem sum_odds (n : ℕ) : ∑ i ∈ Finset.range n, (2 * i + 1) = n ^ 2 := by sorry",
    }
    # The example picker lives OUTSIDE the form so choosing one refills the editor immediately.
    preset = mo.ui.dropdown(list(PRESETS), value=list(PRESETS)[0], label="**example** (fills the editor)")
    preset
    return PRESETS, preset


@app.cell
def _(PRESETS, mo, preset):
    # A form: nothing runs until you hit submit — so editing the theorem doesn't re-fire a proof
    # on every keystroke. The editor's default tracks the example picked above.
    form = (
        mo.md(
            """
            {tool}  &nbsp; {model}

            {stmt}
            """
        )
        .batch(
            tool=mo.ui.dropdown(["draft-sketch-prove (decompose)", "prove (Pass@k whole-proof)"],
                                value="draft-sketch-prove (decompose)", label="tool"),
            model=mo.ui.dropdown(
                ["anthropic/claude-opus-4.5", "anthropic/claude-opus-4.1",
                 "moonshotai/kimi-k2.6", "deepseek/deepseek-v3.2"],
                value="anthropic/claude-opus-4.5", label="model (Opus strongest; others cheaper)"),
            stmt=mo.ui.text_area(value=PRESETS[preset.value], rows=6, full_width=True,
                                 label="Lean target — `theorem … := by sorry` (edit or paste your own)"),
        )
        .form(submit_button_label="▶ prove it")
    )
    form
    return (form,)


@app.cell
async def _(form, mo, prove, prove_by_sketch):
    mo.stop(form.value is None,
            mo.callout("Pick an example (or paste a theorem), choose a tool, and hit **▶ prove it**.",
                       kind="neutral"))
    v = form.value
    target = (v["stmt"] or "").strip()
    with mo.status.spinner(title="drafting · compiling · proving… (~30–70s)"):
        if v["tool"].startswith("draft-sketch"):
            result = await prove_by_sketch(target, model=v["model"], leaf_k=1, sketch_rounds=2)
            md = result.markdown
        else:
            res = await prove(target, k=4, model=v["model"])      # prove() → Job; await → Result
            md = res.markdown
    mo.md(md)
    return
