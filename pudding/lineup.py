"""Resolve friendly model names → (provider, model_id) via contenders.jsonl — the single
swappable lineup shared with the audition and the workbench (one source of truth).

A name is the slug of a row's model *display* (e.g. 'deepseek-v4-pro' from "DeepSeek-V4-Pro · CoT");
an 'org/model' string passes through as a raw OpenRouter id. Generalist rows only (the studio
fans out cot chains); the specialist/TIR row is not a studio lane.
"""
import json
import re
from pathlib import Path

_LINEUP_PATH = Path(__file__).resolve().parent.parent / "contenders.jsonl"
_GENERALIST = {"cot", "self_verify"}
_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG.sub("-", s.lower()).strip("-")


def _load() -> dict[str, tuple[str | None, str]]:
    rows = []
    if _LINEUP_PATH.exists():
        for line in _LINEUP_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    out: dict[str, tuple[str | None, str]] = {}
    for r in rows:
        if r.get("strategy", "cot") in _GENERALIST:
            disp = (r.get("label") or r["model"]).split("·")[0].strip()
            out.setdefault(_slug(disp), (r.get("provider"), r["model"]))
    return out


_MAP = _load()


def resolve(name: str, provider: str | None = None) -> tuple[str | None, str]:
    """(provider, model_id) for a friendly lineup name, or a raw 'org/model' id."""
    if name in _MAP:
        prov, model = _MAP[name]
        return (provider or prov), model
    if "/" in name:                       # raw org/model id (e.g. "deepseek/deepseek-v4-pro")
        return (provider or "openrouter"), name
    raise ValueError(f"unknown model {name!r}; known: {', '.join(sorted(_MAP)) or '(none)'} "
                     f"(or pass an 'org/model' id)")


def default_models() -> list[str]:
    """The audition's recommended generalists (FINDINGS.md): DeepSeek (value) + Qwen (accuracy)."""
    pref = [n for n in ("deepseek-v4-pro", "qwen3-7-max") if n in _MAP]
    return pref or list(_MAP)[:2]
