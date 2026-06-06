"""The public verbs (P1: `solve`, `get`).

`solve` resolves models against the lineup, builds the (model × seed) lanes, and returns a `Job`
immediately — scheduling it on the running event loop if there is one, else lazily (the sync
`.result()` path runs it). `backend="local"` only in P1 ('modal' is the prover's future,
STUDIO_PLAN §2 — Modal can't raise a rented provider's rate-limit ceiling).
"""
import asyncio
import hashlib
import json
import uuid

from . import lineup, store
from .jobs import Job, Lane, Result, result_from_dict, result_to_dict

DEFAULT_MAX_TOKENS = 16384         # thinking shares the budget; a stingy cap empties \boxed{}
DEFAULT_CONCURRENCY = 16           # exploit online-inference parallelism — OpenRouter handles ~20
                                   # inflight (the audition ran at 16); stingy providers set a lower
                                   # per-row `concurrency` in contenders.jsonl. NOT a Modal job.
DEFAULT_MODELS = lineup.default_models()


def solve(problem: str, *, k: int = 5, models: list[str] | None = None, strategy: str = "cot",
          max_tokens: int | None = None, concurrency: int = DEFAULT_CONCURRENCY,
          timeout: float | None = None, backend: str = "local", provider: str | None = None,
          on_event=None, sem=None) -> Job:
    """Fan out k seeded samples per model, vote + cluster the answers → a `Job`.

    job = pudding.solve("…", k=8, models=["deepseek-v4-pro", "qwen3-7-max"])
    result = await job        # async      |   result = pudding.solve("…").result()   # sync

    `timeout` is a per-attempt wall-clock cap (seconds) — set it for interactive use so a
    network/engine stall fails fast instead of hanging on the HTTP-layer timeout.
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
            "max_tokens": max_tokens, "concurrency": concurrency, "timeout": timeout}
    job = Job(uuid.uuid4().hex[:8], spec, lanes, on_event=on_event, sem=sem)
    store.write(job.id, job.to_dict())             # persist as pending so the id is collectable
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        job._schedule(loop)
    return job


def solve_many(problems: list[str], *, k: int = 2, models: list[str] | None = None,
               strategy: str = "cot", timeout: float | None = None,
               concurrency: int = DEFAULT_CONCURRENCY, provider: str | None = None) -> list[Job]:
    """Launch one solve per problem, all sharing ONE rate budget (a single Semaphore sized at the
    provider's ceiling — NOT N × per-job). Returns the Job handles **immediately** (each scheduled
    on the running loop); poll `job.summary()` / `job.completed` for non-blocking live feedback —
    don't await them in the launching cell (STUDIO_PLAN P6). The explore-a-space fan-out."""
    shared = asyncio.Semaphore(concurrency)
    return [solve(p, k=k, models=models, strategy=strategy, timeout=timeout,
                  concurrency=concurrency, provider=provider, sem=shared) for p in problems]


def get(job_id: str) -> Job | None:
    """Reload a persisted job by id (reconnect from a cron / agent / reopened notebook)."""
    d = store.read(job_id)
    return Job.from_dict(d) if d else None


def recent(n: int = 10) -> list[dict]:
    """Recent runs as summaries (newest first) — the reuse browser's source. Reads the job store;
    each row: {id, problem, answer, agreement, status, created}. The id is the durable handle
    (reload with `get`); the artifact, not the cell, is the unit of work (STUDIO_PLAN P5)."""
    out = []
    for jid in store.list_ids():
        d = store.read(jid)
        if not d:
            continue
        r = d.get("result") or {}
        prov = r.get("provenance") or {}
        out.append({"id": jid,
                    "problem": (prov.get("problem") or d.get("spec", {}).get("problem") or "")[:70],
                    "answer": r.get("answer"), "agreement": f"{r.get('count', 0)}/{r.get('n_answered', 0)}",
                    "status": d.get("status"), "created": prov.get("created") or 0.0})
    out.sort(key=lambda s: s["created"], reverse=True)
    return out[:n]


def _content_id(result: Result) -> str:
    """Stable content address for a run — its inputs + per-sample answers + the verdict, minus
    the wall-clock (so identical content addresses identically)."""
    p = result.provenance
    key = {"problem": p.get("problem"), "k": p.get("k"), "models": p.get("models"),
           "strategy": p.get("strategy"), "answer": result.answer,
           "samples": sorted((a.get("model"), a.get("seed"), a.get("boxed"))
                             for a in p.get("attempts", []))}
    blob = json.dumps(key, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def pin(result: Result) -> Result:
    """Freeze a (stochastic) run into a content-addressed, provenance-stamped artifact, persisted
    for reproducible re-render — the marimo-lab → Quarto-publication hop (STUDIO_PLAN §3). Returns
    the same Result with `.pin` set; reload with `get_pin(id)`."""
    result.pin = _content_id(result)
    store.write_pin(result.pin, result_to_dict(result))
    return result


def get_pin(pin_id: str) -> Result | None:
    """Reload a pinned (frozen) result by its content id — re-renders identically."""
    d = store.read_pin(pin_id)
    return result_from_dict(d) if d else None
