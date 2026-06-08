"""Job persistence — plain JSON under results/jobs/ (results/ is gitignored).

A Job survives the client process: an agent/cron/reopened notebook submits, detaches, and
collects by id (`pudding.get`). Pure IO — no `pudding` imports, so jobs.py depends on this
without a cycle. sqlite / Modal Dict can replace it later (STUDIO_PLAN §6) behind read/write.

The per-run JSON files are the source of truth; a sibling `<jobs>-index.json` is a **cache** of
row summaries, upserted on every `write` so the run-management browser (`recent`) lists in O(index)
instead of rescanning + parsing every file (SOLVER_UX_PLAN P7). The cache is rebuildable from the
files at any time via `reindex()`; if it's missing, `summaries()` heals it on first use. (A sqlite
index would scale further — kept as a future swap behind these same functions.)
"""
import json
import os
import tempfile
from pathlib import Path

_RESULTS = Path(__file__).resolve().parent.parent / "results"
_ROOT = Path(os.environ.get("PUDDING_JOBS_DIR", _RESULTS / "jobs"))
_PINS = Path(os.environ.get("PUDDING_PINS_DIR", _RESULTS / "pins"))
_INDEX = _ROOT.with_name(_ROOT.name + "-index.json")    # sibling of jobs/ — NOT inside it (list_ids globs *.json)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + os.replace — a crash mid-write can never leave a half-written file
    that later breaks a read/scan (R2). Used for both run files and the index."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)                            # atomic rename on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write(root: Path, id: str, data: dict) -> None:
    _atomic_write(root / f"{id}.json", json.dumps(data, indent=2))


def _read(root: Path, id: str) -> dict | None:
    """None if missing OR corrupt — a single truncated file must not crash a full `recent()`
    scan (corrupt ≡ missing; the durable unit is recoverable by re-running, not by guessing)."""
    p = root / f"{id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def write(job_id: str, data: dict) -> None:
    _write(_ROOT, job_id, data)
    _index_upsert(job_id, _summarize(data))              # keep the browser cache current


def read(job_id: str) -> dict | None:
    return _read(_ROOT, job_id)


def write_pin(pin_id: str, data: dict) -> None:
    _write(_PINS, pin_id, data)


def read_pin(pin_id: str) -> dict | None:
    return _read(_PINS, pin_id)


def list_ids() -> list[str]:
    return sorted(p.stem for p in _ROOT.glob("*.json")) if _ROOT.exists() else []


# --- the run-management index (a rebuildable cache of row summaries) --------------------------
def _summarize(d: dict) -> dict:
    """Project a persisted job dict → a browser/index row. Pure (mirrors the stored shape written
    by jobs.to_dict); no pudding import."""
    r = d.get("result") or {}
    prov = r.get("provenance") or {}
    spec = d.get("spec") or {}
    return {"id": d.get("id"),
            "problem": (prov.get("problem") or spec.get("problem") or "")[:80],
            "answer": r.get("answer"),
            "count": r.get("count", 0), "n_answered": r.get("n_answered", 0),
            "status": d.get("status"), "created": prov.get("created") or 0.0,
            "tokens": r.get("tokens", 0), "cost": r.get("cost"),
            "k": r.get("k") or spec.get("k"),
            "models": r.get("models") or spec.get("model_names") or []}


def _load_index() -> dict:
    if not _INDEX.exists():
        return {}
    try:
        d = json.loads(_INDEX.read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _index_upsert(job_id: str, summary: dict) -> None:
    try:                                                 # the index is a cache — never break a write
        idx = _load_index()
        idx[job_id] = summary
        _atomic_write(_INDEX, json.dumps(idx, indent=2))
    except Exception:
        pass


def summaries() -> list[dict]:
    """All run summaries from the index (the browser's source — no full-dir rescan). Heals the
    cache from the files if it's absent but runs exist (first use after an upgrade / external write)."""
    idx = _load_index()
    if not idx and _ROOT.exists() and any(_ROOT.glob("*.json")):
        idx = reindex()
    return list(idx.values())


def reindex() -> dict:
    """Rebuild the index from the per-run files (the source of truth) and persist it. Returns the
    fresh id→summary map. Use to recover from a deleted/stale index."""
    idx = {}
    for jid in list_ids():
        d = _read(_ROOT, jid)
        if d:
            idx[jid] = _summarize(d)
    _atomic_write(_INDEX, json.dumps(idx, indent=2))
    return idx


def delete(job_id: str) -> bool:
    """Delete a run: remove its file AND its index entry. Returns True if anything was removed."""
    removed = False
    p = _ROOT / f"{job_id}.json"
    if p.exists():
        try:
            p.unlink()
            removed = True
        except OSError:
            pass
    idx = _load_index()
    if job_id in idx:
        del idx[job_id]
        _atomic_write(_INDEX, json.dumps(idx, indent=2))
        removed = True
    return removed
