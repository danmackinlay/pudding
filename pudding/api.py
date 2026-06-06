"""The public verbs (P1: `solve`, `get`).

`solve` resolves models against the lineup, builds the (model × seed) lanes, and returns a `Job`
immediately — scheduling it on the running event loop if there is one, else lazily (the sync
`.result()` path runs it). `backend="local"` only in P1 ('modal' is the prover's future,
STUDIO_PLAN §2 — Modal can't raise a rented provider's rate-limit ceiling).
"""
import asyncio
import uuid

from . import lineup, store
from .jobs import Job, Lane

DEFAULT_MAX_TOKENS = 16384         # thinking shares the budget; a stingy cap empties \boxed{}
DEFAULT_CONCURRENCY = 8
DEFAULT_MODELS = lineup.default_models()


def solve(problem: str, *, k: int = 5, models: list[str] | None = None, strategy: str = "cot",
          max_tokens: int | None = None, concurrency: int = DEFAULT_CONCURRENCY,
          backend: str = "local", provider: str | None = None, on_event=None) -> Job:
    """Fan out k seeded samples per model, vote + cluster the answers → a `Job`.

    job = pudding.solve("…", k=8, models=["deepseek-v4-pro", "qwen3-7-max"])
    result = await job        # async      |   result = pudding.solve("…").result()   # sync
    """
    if backend != "local":
        raise NotImplementedError("backend='modal' is deferred to the prover (STUDIO_PLAN §2)")
    names = list(models) if models else DEFAULT_MODELS
    if not names:
        raise ValueError("no models — pass models=[...] or populate contenders.jsonl")
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    lanes = [Lane(name=name, provider=prov, model=model_id, strategy=strategy, seed=seed,
                  temperature=0.0 if k == 1 else 0.6)
             for name in names
             for prov, model_id in [lineup.resolve(name, provider)]
             for seed in range(k)]
    spec = {"problem": problem, "k": k, "model_names": names, "strategy": strategy,
            "max_tokens": max_tokens, "concurrency": concurrency}
    job = Job(uuid.uuid4().hex[:8], spec, lanes, on_event=on_event)
    store.write(job.id, job.to_dict())             # persist as pending so the id is collectable
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        job._schedule(loop)
    return job


def get(job_id: str) -> Job | None:
    """Reload a persisted job by id (reconnect from a cron / agent / reopened notebook)."""
    d = store.read(job_id)
    return Job.from_dict(d) if d else None
