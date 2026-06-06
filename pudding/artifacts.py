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
