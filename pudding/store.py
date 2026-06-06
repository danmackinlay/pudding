"""Job persistence — plain JSON under results/jobs/ (results/ is gitignored).

A Job survives the client process: an agent/cron/reopened notebook submits, detaches, and
collects by id (`pudding.get`). Pure IO — no `pudding` imports, so jobs.py depends on this
without a cycle. sqlite / Modal Dict can replace it later (STUDIO_PLAN §6) behind read/write.
"""
import json
import os
from pathlib import Path

_ROOT = Path(os.environ.get(
    "PUDDING_JOBS_DIR", Path(__file__).resolve().parent.parent / "results" / "jobs"))


def _path(job_id: str) -> Path:
    return _ROOT / f"{job_id}.json"


def write(job_id: str, data: dict) -> None:
    _ROOT.mkdir(parents=True, exist_ok=True)
    _path(job_id).write_text(json.dumps(data, indent=2))


def read(job_id: str) -> dict | None:
    p = _path(job_id)
    return json.loads(p.read_text()) if p.exists() else None


def list_ids() -> list[str]:
    return sorted(p.stem for p in _ROOT.glob("*.json")) if _ROOT.exists() else []
