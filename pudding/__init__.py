"""pudding — the maths studio library (STUDIO_PLAN Phase C, P1).

Headless, zero frontend deps: the async ``solve()`` your own code can invoke. A `Job` fans out k
seeded samples per model (local asyncio), **keeps every attempt**, clusters answers (within- and
cross-model agreement), renders a canonical markdown artifact, and persists so you can detach and
reconnect with ``pudding.get(id)``. Interactivity is opt-in (``on_event`` / ``job.stream()``).

    import pudding
    res = pudding.solve("Find the remainder when 7^999 is divided by 1000.", k=5).result()
    print(res.answer, res.agreement, res.markdown)        # sync

    job = pudding.solve("…", k=8, models=["deepseek-v4-pro", "qwen3-7-max"])
    res = await job                                        # async; or `async for e in job.stream()`
"""
from .api import (DEFAULT_MODELS, conjecture, delete, discover, falsify, get, get_pin, pin,
                  recent, solve, solve_many)
from .artifacts import answer_text, render, to_html, verdict, view_model
from .discovery import Conjecture, Flock, flock_view_model
from .jobs import Attempt, Cluster, Job, Result

__all__ = ["solve", "solve_many", "get", "recent", "delete", "pin", "get_pin", "render",
           "view_model", "to_html", "answer_text", "verdict", "Job", "Result", "Attempt",
           "Cluster", "DEFAULT_MODELS", "conjecture", "falsify", "discover", "Conjecture",
           "Flock", "flock_view_model"]
