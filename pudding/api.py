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
from .discovery import Flock, conjecture_async, discover_async, falsify_async
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
    temperature = 0.0 if k == 1 else 0.6           # k==1 → deterministic; k>1 → diverge the chains
    lanes = [Lane(name=name, provider=prov, model=model_id, strategy=strategy, seed=seed,
                  temperature=temperature)
             for name in names
             for prov, model_id in [lineup.resolve(name, provider)]
             for seed in range(k)]
    spec = {"problem": problem, "k": k, "model_names": names, "strategy": strategy,
            "max_tokens": max_tokens, "concurrency": concurrency, "timeout": timeout,
            "temperature": temperature}
    job = Job(uuid.uuid4().hex[:12], spec, lanes, on_event=on_event, sem=sem)   # 48 bits — headroom
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
    don't await them in the launching cell (STUDIO_PLAN P6). The explore-a-space fan-out.

    Requires a running event loop: the shared Semaphore binds to it, and the launch-don't-await
    contract (handles you poll, not block on) only means anything inside one. A sync script should
    `asyncio.run(...)` an async wrapper, or call `solve(...).result()` per problem."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError(
            "solve_many() needs a running event loop (it shares one Semaphore across the batch and "
            "returns handles to poll). Call it inside async code / a marimo cell, or wrap with "
            "asyncio.run(); for one-off sync use, solve(p).result() per problem.") from None
    shared = asyncio.Semaphore(concurrency)        # binds to the running loop; all jobs share it
    return [solve(p, k=k, models=models, strategy=strategy, timeout=timeout,
                  concurrency=concurrency, provider=provider, sem=shared) for p in problems]


# --- the generative loop (P3): AI proposes, the oracle disposes, you curate ----
async def conjecture(context: str, *, n: int = 8, models: list[str] | None = None,
                     provider: str | None = None, temperature: float = 0.8,
                     on_event=None) -> Flock:
    """Propose `n` falsifiable conjectures from `context` (a selection, a corpus, raw data) →
    a `Flock` of `proposed` claims, each carrying a `counterexample()` harness. Run `falsify`
    next. Async (quick): `await pudding.conjecture(...)`, or `asyncio.run(...)` headless."""
    return await conjecture_async(context, n=n, models=models, provider=provider,
                                  temperature=temperature, on_event=on_event)


async def falsify(flock: Flock, *, timeout: float = 8.0, concurrency: int = 8,
                  on_event=None) -> Flock:
    """Run each conjecture's harness through the cheap oracle in parallel → the flock thinned
    (`refuted` / `survives` / `error`). The `flock.survivors` are the candidates worth proving —
    feed `s.statement` to `solve("Prove or disprove: …")`. **Surviving ≠ proven.**"""
    return await falsify_async(flock, timeout=timeout, concurrency=concurrency, on_event=on_event)


async def discover(context: str, *, n: int = 8, models: list[str] | None = None,
                   provider: str | None = None, timeout: float = 8.0, concurrency: int = 8,
                   on_event=None) -> Flock:
    """conjecture → falsify in one call → a flock whose `.survivors` survived the oracle."""
    return await discover_async(context, n=n, models=models, provider=provider, timeout=timeout,
                                concurrency=concurrency, on_event=on_event)


def get(job_id: str) -> Job | None:
    """Reload a persisted job by id (reconnect from a cron / agent / reopened notebook)."""
    d = store.read(job_id)
    return Job.from_dict(d) if d else None


_SORT_KEYS = {"created", "tokens", "answer", "status", "problem"}


def recent(n: int = 10, *, status: str | None = None, query: str | None = None,
           sort: str = "created", desc: bool = True) -> list[dict]:
    """Recent runs as summaries — the run-management browser's source (SOLVER_UX_PLAN P7). Reads
    the O(index) cache (no full-dir rescan), then filters/sorts in memory. Each row:
    {id, problem, answer, agreement, status, created, tokens, cost, k, models}.

    `status` keeps only that status (done/error/cancelled/running/pending); `query` is a
    case-insensitive substring over the problem; `sort` ∈ {created,tokens,answer,status,problem}.
    The id is the durable handle (reload with `get`, drop with `delete`)."""
    q = (query or "").lower().strip()
    rows = []
    for s in store.summaries():
        if status and s.get("status") != status:
            continue
        problem = s.get("problem") or ""
        if q and q not in problem.lower():
            continue
        rows.append({"id": s.get("id"), "problem": problem, "answer": s.get("answer"),
                     "agreement": f"{s.get('count', 0)}/{s.get('n_answered', 0)}",
                     "status": s.get("status"), "created": s.get("created") or 0.0,
                     "tokens": s.get("tokens", 0), "cost": s.get("cost"),
                     "k": s.get("k"), "models": s.get("models") or []})
    key = sort if sort in _SORT_KEYS else "created"
    numeric = key in ("created", "tokens")
    rows.sort(key=lambda r: (r.get(key) or 0) if numeric else str(r.get(key) or ""), reverse=desc)
    return rows[:n] if n else rows


def delete(run_id: str) -> bool:
    """Forget a run: remove its stored file and its index entry. Returns True if anything was
    removed. (Pins are content-addressed frozen artifacts and are not touched here.)"""
    return store.delete(run_id)


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
