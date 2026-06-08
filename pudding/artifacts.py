"""Result → canonical markdown artifact (+ a per-frontend render adapter), and cost.

Canonical math is ``$…$`` (paper/Quarto-native); ``render(result, target="owui")`` rewrites to
``\\(…\\)`` for Open WebUI. Cost from prices.json. Duck-types the Result (no jobs import → no
cycle); no frontend deps. STUDIO_PLAN §2-3: the library owns the static render; live widgets live
in studio/.
"""
import json
import os
import re
from pathlib import Path

from streaming import normalize_delimiters

# An answer may be a number ("143"), a math expr ("\frac{3}{56}"), LaTeX prose ("\text{true}"), or
# plain prose ("the statement is true"). MathJax surfaces (mo.md) render the first three when wrapped
# in $…$, but $plain prose$ comes out as ugly italic math — so wrap ONLY when the answer is math-y.
# Plain-text surfaces (table cells, dropdowns) can't render LaTeX at all → `answer_text` cleans it.
_MATHY = re.compile(r"[\\^_={}]")
_TEX_WRAP = re.compile(r"\\(?:text|mathrm|mathbf|mathit|operatorname|boxed)\s*\{([^{}]*)\}")


def _is_number(s: str) -> bool:
    try:
        float(str(s).replace(",", "").strip())
        return True
    except (ValueError, AttributeError):
        return False


def _is_mathy(s: str) -> bool:
    s = s or ""
    return _is_number(s) or bool(_MATHY.search(s))


def _answer_md(s) -> str:
    """Render an answer for a MathJax markdown surface: $…$ if math-y, else plain prose."""
    s = "" if s is None else str(s)
    return f"${s}$" if _is_mathy(s) else s


def _answer_html(s) -> str:
    s = "" if s is None else str(s)
    if _is_mathy(s):
        return f"\\({s}\\)"
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def answer_text(s) -> str:
    """A plain-text rendering of a boxed answer for non-MathJax surfaces (table cells, dropdowns):
    unwrap \\text{}/\\boxed{} etc., drop $ delimiters, collapse whitespace. Best-effort — residual
    math (e.g. \\frac{…}) is left as-is; the full LaTeX still renders in the markdown board."""
    s = "" if s is None else str(s)
    prev = None
    while prev != s:                       # unwrap nested \text{…}/\boxed{…} a few passes
        prev = s
        s = _TEX_WRAP.sub(r"\1", s)
    s = s.replace("$", "").replace("\\,", " ").replace("\\ ", " ")
    return re.sub(r"\s+", " ", s).strip() or "∅"


# A prove-or-disprove answer is a verdict, not a value. `decide` mode (api.solve) elicits a
# canonical \boxed{True|False|Unknown} token → the exact-token branch makes clustering reliable.
# The fuzzy phrase fallback is CONSERVATIVE: it labels only unambiguous single-signal prose and
# returns None otherwise, so it never confidently mislabels (and numeric answers are never hijacked).
_FALSE_NEG = re.compile(r"\b(not true|does not hold|doesn'?t hold|fails? to hold|cannot hold)\b")
_TRUE_RE = re.compile(r"\b(true|holds?|valid|proven|proved|correct)\b")
_FALSE_RE = re.compile(r"\b(false|disprov\w*|counterexample|fails?|invalid|incorrect)\b")


def verdict(s) -> str | None:
    """Normalize a decision answer to 'true' / 'false' / 'unknown', or None if it isn't verdict-like.
    Canonical tokens (what `decide` mode produces) match exactly; free prose is best-effort — a
    negation-of-truth phrase ('not true' / 'does not hold') is decisively false, then a lone true/
    false signal decides, else None (no guess → numeric answers aren't hijacked)."""
    t = answer_text(s).strip().lower().rstrip(".!")
    if not t:
        return None
    if t in ("true", "false", "unknown"):
        return t
    if t in ("undetermined", "undecided", "indeterminate", "open", "unproven", "cannot be determined"):
        return "unknown"
    if _FALSE_NEG.search(t):                 # "not true" / "does not hold" → false, decisively
        return "false"
    f, tr = bool(_FALSE_RE.search(t)), bool(_TRUE_RE.search(t))
    if f and not tr:
        return "false"
    if tr and not f:
        return "true"
    return None                              # ambiguous/none → fall back to normal string clustering


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
        head = f"**{_answer_md(result.answer)}**  ·  agreement {result.count}/{result.n_answered}{cross}"
    lines = [head, ""]
    if len(result.clusters) > 1:
        lines.append("distribution:")
        for c in result.clusters:
            lines.append(f"- {_answer_md(c.answer)} ×{c.count}  ({', '.join(c.models)})")
        lines.append("")
    lines.append(_footer(result))
    return "\n".join(lines)


def _footer(result) -> str:
    parts = [f"— maj@{result.k} · {', '.join(result.models)}", f"{result.tokens} tok"]
    if result.cost is not None:
        parts.append(f"~${result.cost:.4f}")
    return " · ".join(parts)


_RENDER_TARGETS = ("plain", "quarto", "owui")


def render(obj, target: str = "plain") -> str:
    """Adapt the canonical artifact to a frontend's math dialect. `obj` is a Result or markdown.
    'plain'/'quarto' keep the canonical ``$…$`` (paper/Quarto-native — no transform yet); 'owui'
    rewrites inline ``$…$`` → ``\\(…\\)`` for Open WebUI. Unknown targets raise (no silent passthrough)."""
    if target not in _RENDER_TARGETS:
        raise ValueError(f"unknown render target {target!r}; known: {', '.join(_RENDER_TARGETS)}")
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
            f"<p><b>{_answer_html(vm['answer'])}</b> · agreement {vm['agreement']}"
            f"{' · cross-model ✓' if vm['cross_model'] else ''}</p>")
    rows = "".join(f"<tr><td>{_answer_html(c['answer'])}</td><td>{c['count']}</td>"
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
