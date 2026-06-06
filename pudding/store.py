"""Job persistence — plain JSON under results/jobs/ (results/ is gitignored).

A Job survives the client process: an agent/cron/reopened notebook submits, detaches, and
collects by id (`pudding.get`). Pure IO — no `pudding` imports, so jobs.py depends on this
without a cycle. sqlite / Modal Dict can replace it later (STUDIO_PLAN §6) behind read/write.
"""
import json
import os
import tempfile
from pathlib import Path

_RESULTS = Path(__file__).resolve().parent.parent / "results"
_ROOT = Path(os.environ.get("PUDDING_JOBS_DIR", _RESULTS / "jobs"))
_PINS = Path(os.environ.get("PUDDING_PINS_DIR", _RESULTS / "pins"))


def _write(root: Path, id: str, data: dict) -> None:
    """Atomic write: serialize to a temp file in the same dir, then os.replace — so a crash
    mid-write can never leave a half-written .json that later breaks `_read`/`recent`."""
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=f".{id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        os.replace(tmp, root / f"{id}.json")     # atomic rename on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def read(job_id: str) -> dict | None:
    return _read(_ROOT, job_id)


def write_pin(pin_id: str, data: dict) -> None:
    _write(_PINS, pin_id, data)


def read_pin(pin_id: str) -> dict | None:
    return _read(_PINS, pin_id)


def list_ids() -> list[str]:
    return sorted(p.stem for p in _ROOT.glob("*.json")) if _ROOT.exists() else []
