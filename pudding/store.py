"""Job persistence — plain JSON under results/jobs/ (results/ is gitignored).

A Job survives the client process: an agent/cron/reopened notebook submits, detaches, and
collects by id (`pudding.get`). Pure IO — no `pudding` imports, so jobs.py depends on this
without a cycle. sqlite / Modal Dict can replace it later (STUDIO_PLAN §6) behind read/write.
"""
import json
import os
from pathlib import Path

_RESULTS = Path(__file__).resolve().parent.parent / "results"
_ROOT = Path(os.environ.get("PUDDING_JOBS_DIR", _RESULTS / "jobs"))
_PINS = Path(os.environ.get("PUDDING_PINS_DIR", _RESULTS / "pins"))


def _write(root: Path, id: str, data: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{id}.json").write_text(json.dumps(data, indent=2))


def _read(root: Path, id: str) -> dict | None:
    p = root / f"{id}.json"
    return json.loads(p.read_text()) if p.exists() else None


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
