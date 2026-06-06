"""Result → canonical markdown artifact (+ a per-frontend render adapter), and cost.

Canonical math is ``$…$`` (paper/Quarto-native); ``render(result, target="owui")`` rewrites to
``\\(…\\)`` for Open WebUI. Cost from prices.json. Duck-types the Result (no jobs import → no
cycle); no frontend deps. STUDIO_PLAN §2-3: the library owns the static render; live widgets live
in studio/.
"""
import json
import os
from pathlib import Path

from streaming import normalize_delimiters

_PRICES_PATH = Path(os.environ.get(
    "WORKBENCH_PRICES", Path(__file__).resolve().parent.parent / "prices.json"))


def _load_prices() -> dict:
    try:
        data = json.loads(_PRICES_PATH.read_text())
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


_PRICES = _load_prices()


def est_cost(model_id: str, tokens: int):
    """Estimated $ for `tokens` output at `model_id`'s price, or None if unknown/free."""
    out = (_PRICES.get(model_id) or {}).get("out")
    return tokens * out / 1e6 if out and out > 0 else None


def est_cost_total(attempts) -> float | None:
    total, known = 0.0, False
    for a in attempts:
        c = est_cost(a.model_id, a.tokens)
        if c is not None:
            total += c
            known = True
    return total if known else None


def to_markdown(result) -> str:
    """Canonical markdown: the voted answer + agreement (cross-model flagged), the cluster
    distribution when there's dissent, and a maj@k · tokens · est-$ footer."""
    if result.answer is None:
        errs = [a.error for a in result.attempts if a.error]
        if errs and len(errs) == result.n_total:        # outage / engine failure, not "unsolved"
            head = f"**failed** — all {result.n_total} samples errored: `{errs[0]}`"
        elif errs:
            head = (f"**No answer** — {result.n_answered}/{result.n_total} answered; "
                    f"{len(errs)} errored (`{errs[0]}`)")
        else:
            head = "**No answer** — the samples produced no boxed result."
    else:
        cross = " · cross-model ✓" if result.cross_model else ""
        head = f"**${result.answer}$**  ·  agreement {result.count}/{result.n_answered}{cross}"
    lines = [head, ""]
    if len(result.clusters) > 1:
        lines.append("distribution:")
        for c in result.clusters:
            lines.append(f"- ${c.answer}$ ×{c.count}  ({', '.join(c.models)})")
        lines.append("")
    lines.append(_footer(result))
    return "\n".join(lines)


def _footer(result) -> str:
    parts = [f"— maj@{result.k} · {', '.join(result.models)}", f"{result.tokens} tok"]
    if result.cost is not None:
        parts.append(f"~${result.cost:.4f}")
    return " · ".join(parts)


def render(obj, target: str = "plain") -> str:
    """Adapt the canonical artifact to a frontend's math dialect. `obj` is a Result or markdown.
    target: 'plain'/'quarto' keep ``$…$``; 'owui' rewrites to ``\\(…\\)`` (Open WebUI)."""
    md = obj.markdown if hasattr(obj, "markdown") else str(obj)
    return normalize_delimiters(md) if target == "owui" else md


# --- view-model + static render (the library owns these; live widgets live in studio/) ------
def view_model(result) -> dict:
    """The data a frontend renders, as a plain dict — the answer-cluster board's rows + summary.
    A reactive shell (marimo) wraps this in controls; a static doc embeds `to_html`. Decision #9."""
    return {
        "answer": result.answer,
        "agreement": f"{result.count}/{result.n_answered}" if result.n_answered else "0/0",
        "agreement_frac": result.agreement,
        "cross_model": result.cross_model,
        "tokens": result.tokens,
        "cost": result.cost,
        "k": result.k,
        "models": list(result.models),
        "pin": getattr(result, "pin", None),
        "clusters": [{"answer": c.answer, "count": c.count, "models": list(c.models)}
                     for c in result.clusters],
        "attempts": [{"model": a.model, "seed": a.seed, "boxed": a.boxed, "tokens": a.tokens,
                      "error": a.error, "transcript": a.transcript,
                      "thinking": getattr(a, "thinking", "")} for a in result.attempts],
    }


def to_html(result) -> str:
    """A minimal static render of the answer-cluster board (for Quarto / any non-interactive
    embed): the headline, a cluster table, and collapsible per-attempt transcripts."""
    vm = view_model(result)
    head = ("<p><b>No answer</b> — the samples produced no boxed result.</p>"
            if vm["answer"] is None else
            f"<p><b>\\({vm['answer']}\\)</b> · agreement {vm['agreement']}"
            f"{' · cross-model ✓' if vm['cross_model'] else ''}</p>")
    rows = "".join(f"<tr><td>\\({c['answer']}\\)</td><td>{c['count']}</td>"
                   f"<td>{', '.join(c['models'])}</td></tr>" for c in vm["clusters"])
    table = (f"<table><thead><tr><th>answer</th><th>votes</th><th>models</th></tr></thead>"
             f"<tbody>{rows}</tbody></table>" if vm["clusters"] else "")
    cost = f" · ~${vm['cost']:.4f}" if vm["cost"] is not None else ""
    foot = f"<p><small>— maj@{vm['k']} · {', '.join(vm['models'])} · {vm['tokens']} tok{cost}</small></p>"
    details = "".join(
        f"<details><summary>{a['model']} #{a['seed']} → "
        f"{a['boxed'] if a['boxed'] is not None else '∅'}</summary>"
        f"<pre>{(a.get('thinking') + chr(10) + '---' + chr(10) if a.get('thinking') else '')}"
        f"{a['transcript'] or a['error'] or ''}</pre></details>" for a in vm["attempts"])
    return f"<div class='pudding-board'>{head}{table}{foot}{details}</div>"
