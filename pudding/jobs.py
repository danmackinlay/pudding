"""The job layer — the one async/fan-out seam (STUDIO_PLAN §2).

A `Job` fans out k seeded samples per model (local `asyncio.gather` + a `Semaphore` — the
complete fan-out for rented generalists; the provider rate limit is the ceiling), **keeps every
attempt** (the cluster widget needs the distribution, not just the vote), clusters answers by
normalized form (within- and cross-model agreement), renders a markdown artifact, and persists so
the client can detach and reconnect. Interactivity is opt-in: `on_event` / `stream()` observe;
`cancel()` controls — none required to run headless.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field

from solver_loop import solve_one_async        # one chain → {boxed, transcript, tokens, …}
from eval import _norm_latex, _as_number        # reuse the grader's answer normalization
from streaming import ev
from . import store
from .artifacts import to_markdown, est_cost_total

_DONE = object()


# --- records ---------------------------------------------------------------
@dataclass
class Lane:
    """One fan-out unit: a (model × seed) chain. Runtime-only (not persisted; derivable)."""
    name: str          # friendly lineup name (the cluster groups by this)
    provider: str | None
    model: str         # provider model id
    strategy: str
    seed: int
    temperature: float


@dataclass
class Attempt:
    model: str         # friendly lane name
    provider: str | None
    model_id: str
    seed: int
    boxed: str | None
    transcript: str
    tokens: int
    ttft_s: float | None
    decode_tok_s: float | None
    truncated: bool
    error: str | None
    thinking: str = ""     # the model's hidden CoT (reasoning_content), kept for the drill-in


@dataclass
class Cluster:
    answer: str        # a representative raw \boxed{} from the cluster
    key: str           # the normalized clustering key
    count: int
    models: list[str]  # distinct friendly model names that produced it (→ cross-model)


@dataclass
class Result:
    answer: str | None
    count: int             # winner's votes
    n_answered: int        # samples that produced any boxed answer
    n_total: int
    agreement: float       # count / n_answered
    cross_model: bool      # winner produced by >1 distinct model
    clusters: list[Cluster]
    attempts: list[Attempt]
    tokens: int
    cost: float | None
    k: int
    models: list[str]
    markdown: str
    provenance: dict
    pin: str | None = None        # set by pudding.pin() — a content-addressed frozen id


# --- clustering / reduction ------------------------------------------------
def _cluster_key(boxed: str | None):
    if not boxed:
        return None
    s = _norm_latex(boxed).strip().strip("$").strip()   # \boxed{$143$} clusters with \boxed{143}
    num = _as_number(s)
    if num is not None:
        if isinstance(num, float) and num.is_integer():
            num = int(num)
        return f"={num}"            # numeric equivalence (143 == 143.0)
    return s


def _reduce(attempts: list[Attempt], spec: dict) -> Result:
    answered = [a for a in attempts if _cluster_key(a.boxed) is not None]
    by_key: dict[str, list[Attempt]] = {}
    for a in answered:
        by_key.setdefault(_cluster_key(a.boxed), []).append(a)
    clusters = [Cluster(answer=atts[0].boxed, key=k, count=len(atts),
                        models=sorted({a.model for a in atts}))
                for k, atts in by_key.items()]
    clusters.sort(key=lambda c: c.count, reverse=True)
    tokens = sum(a.tokens for a in attempts)
    cost = est_cost_total(attempts)
    base = dict(clusters=clusters, attempts=attempts, tokens=tokens, cost=cost,
                n_total=len(attempts), k=spec["k"], models=spec["model_names"],
                provenance=_prov(spec, attempts))
    if not clusters:
        r = Result(answer=None, count=0, n_answered=0, agreement=0.0, cross_model=False,
                   markdown="", **base)
    else:
        win = clusters[0]
        r = Result(answer=win.answer, count=win.count, n_answered=len(answered),
                   agreement=win.count / len(answered), cross_model=len(win.models) > 1,
                   markdown="", **base)
    r.markdown = to_markdown(r)
    return r


def _error_result(spec: dict, attempts: list[Attempt], msg: str) -> Result:
    return Result(answer=None, count=0, n_answered=0, n_total=len(attempts), agreement=0.0,
                  cross_model=False, clusters=[], attempts=attempts,
                  tokens=sum(a.tokens for a in attempts), cost=est_cost_total(attempts),
                  k=spec["k"], models=spec["model_names"], markdown=f"**error:** {msg}",
                  provenance=_prov(spec, attempts, error=msg))


def _prov(spec: dict, attempts: list[Attempt], error: str | None = None) -> dict:
    p = {"problem": spec["problem"], "k": spec["k"], "models": spec["model_names"],
         "strategy": spec["strategy"], "created": time.time(),
         "attempts": [{"model": a.model, "seed": a.seed, "boxed": a.boxed,
                       "tokens": a.tokens, "error": a.error} for a in attempts]}
    if error:
        p["error"] = error
    return p


# --- (de)serialization (shared by the job store and pin store) -------------
def result_to_dict(r: Result) -> dict:
    return asdict(r)


def result_from_dict(d: dict) -> Result:
    d = dict(d)
    d["clusters"] = [Cluster(**c) for c in d.get("clusters", [])]
    d["attempts"] = [Attempt(**a) for a in d.get("attempts", [])]
    return Result(**d)


# --- the fan-out -----------------------------------------------------------
async def _collect(problem, lanes, *, max_tokens, concurrency, emit, timeout=None,
                   sem=None) -> list[Attempt]:
    sem = sem or asyncio.Semaphore(concurrency)     # shared (batch rate budget) or per-job

    async def one(lane: Lane) -> Attempt:
        async with sem:
            coro = solve_one_async(problem, strategy=lane.strategy, model=lane.model,
                                   provider=lane.provider, temperature=lane.temperature,
                                   seed=lane.seed, max_tokens=max_tokens)
            try:
                r = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            except asyncio.TimeoutError:        # interactive cap — fail fast under connectivity strife
                r = {"boxed": None, "transcript": "", "completion_tokens": 0, "truncated": False,
                     "ttft_s": None, "decode_tok_s": None, "thinking": "",
                     "error": f"timeout ({timeout:g}s)"}
        att = Attempt(model=lane.name, provider=lane.provider, model_id=lane.model, seed=lane.seed,
                      boxed=r.get("boxed"), transcript=r.get("transcript", ""),
                      tokens=r.get("completion_tokens") or 0, ttft_s=r.get("ttft_s"),
                      decode_tok_s=r.get("decode_tok_s"), truncated=bool(r.get("truncated")),
                      error=r.get("error"), thinking=r.get("thinking") or "")
        emit(ev("attempt", lane=att.model, seed=att.seed, boxed=att.boxed, error=att.error))
        return att

    tasks = [asyncio.create_task(one(l)) for l in lanes]
    out = []
    try:
        for fut in asyncio.as_completed(tasks):
            out.append(await fut)
    except asyncio.CancelledError:
        for t in tasks:                            # cancel propagates to children → close in-flight HTTP
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    out.sort(key=lambda a: (a.model, a.seed))     # stable order, independent of completion order
    return out


# --- the Job ---------------------------------------------------------------
class Job:
    """A submitted solve: persistent (`id`), awaitable, streamable, cancellable.

    Async:   job = solve(...);  result = await job
    Sync:    result = solve(...).result()
    Observe: pass on_event=, or `async for e in job.stream()`
    """

    def __init__(self, id: str, spec: dict, lanes: list[Lane] | None = None, on_event=None,
                 sem=None):
        self.id = id
        self.spec = spec
        self.lanes = lanes or []
        self.status = "pending"
        self.attempts: list[Attempt] = []
        self.on_event = on_event
        self._result: Result | None = None
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue | None = None
        self._sem = sem            # shared rate budget across a batch (solve_many); None = own
        self._seen = 0             # live completed-attempt counter (for non-blocking grid polling)
        self._persist_error: str | None = None   # last store.write failure (so it's not silent)

    @property
    def total(self) -> int:
        return len(self.lanes) or (self.spec.get("k", 0) * len(self.spec.get("model_names", [])))

    @property
    def completed(self) -> int:
        return self._seen

    def summary(self) -> dict:
        """Live, poll-safe snapshot for a non-blocking grid — no need to consume the stream."""
        r = self._result
        return {"id": self.id, "problem": (self.spec.get("problem") or "")[:60],
                "status": self.status, "done": f"{self._seen}/{self.total}",
                "answer": r.answer if r else None,
                "agreement": (f"{r.count}/{r.n_answered}" if (r and r.n_answered) else "—")}

    # scheduling / awaiting -------------------------------------------------
    def _schedule(self, loop) -> None:
        if self._task is None and self._result is None:
            if self._queue is None:
                self._queue = asyncio.Queue()      # capture events from the first emit — no race
            self._task = loop.create_task(self._run())
            self.status = "running"

    def __await__(self):
        return self._await().__await__()

    async def _await(self) -> Result:
        if self._result is not None:
            return self._result
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
            self.status = "running"
        await self._task
        return self._result

    def result(self) -> Result:
        """Block for the result (sync contexts). In an event loop, use `await job` instead."""
        if self._result is not None:
            return self._result
        if self._task is not None:
            raise RuntimeError("job is running on an event loop — use `await job` to get the result")
        return asyncio.run(self._run())

    async def stream(self):
        """Yield envelope events as attempts land (opt-in liveness). Drives the run if needed."""
        if self._result is not None:
            return
        if self._queue is None:
            self._queue = asyncio.Queue()
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
            self.status = "running"
        while True:
            e = await self._queue.get()
            if e is _DONE:
                break
            yield e

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self.status = "cancelled"

    async def widen(self, k: int | None = None) -> Result:
        """Buy more confidence: add k more seeded samples per model (continuing the seed
        numbering so they actually diverge), re-cluster, re-render, re-persist. Awaits the
        initial run first if needed. `await job.widen(5)` (sync callers: asyncio.run)."""
        if self._result is None:
            await self._await()
        add = k or self.spec["k"]
        start = max((a.seed for a in self.attempts), default=-1) + 1
        info = {a.model: (a.provider, a.model_id) for a in self.attempts}
        # Widening MUST diverge to add information, so force temperature>0: reuse the run's own
        # temperature, but a deterministic base (k==1 → 0.0) falls back to 0.6 (0.0 is falsy) —
        # else the "extra" samples would be byte-identical and buy no confidence.
        temperature = self.spec.get("temperature") or 0.6
        new_lanes = [Lane(name=n, provider=info.get(n, (None, n))[0],
                          model=info.get(n, (None, n))[1], strategy=self.spec["strategy"],
                          seed=s, temperature=temperature)
                     for n in self.spec["model_names"] for s in range(start, start + add)]
        new = await _collect(self.spec["problem"], new_lanes, max_tokens=self.spec["max_tokens"],
                             concurrency=self.spec["concurrency"], emit=self._emit,
                             timeout=self.spec.get("timeout"), sem=self._sem)
        self.attempts = sorted(self.attempts + new, key=lambda a: (a.model, a.seed))
        self.spec["k"] = self.spec["k"] + add          # maj@k now reflects the wider sample
        self._result = _reduce(self.attempts, self.spec)
        self.status = "done"
        self._emit(ev("widened", added=len(new), answer=self._result.answer))
        self._persist()
        return self._result

    # internals -------------------------------------------------------------
    def _emit(self, event: dict) -> None:
        if event.get("type") == "attempt":
            self._seen += 1        # live progress for poll-based (timer) grids — no stream needed
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:        # a consumer's sink must never break the run
                pass
        if self._queue is not None:
            self._queue.put_nowait(event)

    def _persist(self) -> None:
        """Best-effort store.write — never crash the run, but never silently lose it either:
        a failure lands in `self._persist_error` (and a debug log) so it's discoverable."""
        try:
            store.write(self.id, self.to_dict())
            self._persist_error = None
        except Exception as e:        # noqa: BLE001
            self._persist_error = repr(e)
            logging.getLogger(__name__).debug("job %s persist failed: %r", self.id, e)

    async def _run(self) -> Result:
        self.status = "running"
        try:
            self.attempts = await _collect(self.spec["problem"], self.lanes,
                                           max_tokens=self.spec["max_tokens"],
                                           concurrency=self.spec["concurrency"], emit=self._emit,
                                           timeout=self.spec.get("timeout"), sem=self._sem)
            self._result = _reduce(self.attempts, self.spec)
            self.status = "done"
        except asyncio.CancelledError:
            self.status = "cancelled"
            if self._queue is not None:
                self._queue.put_nowait(_DONE)
            raise
        except Exception as e:       # noqa: BLE001 — surface in the result, never crash the caller
            self._result = _error_result(self.spec, self.attempts, f"{type(e).__name__}: {e}")
            self.status = "error"
        self._emit(ev("done", answer=self._result.answer if self._result else None,
                      status=self.status))
        if self._queue is not None:
            self._queue.put_nowait(_DONE)
        self._persist()
        return self._result

    # persistence -----------------------------------------------------------
    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "spec": self.spec,
                "attempts": [asdict(a) for a in self.attempts],
                "result": asdict(self._result) if self._result is not None else None}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        job = cls(d["id"], d.get("spec", {}), lanes=[], on_event=None)
        job.status = d.get("status", "done")
        job.attempts = [Attempt(**a) for a in d.get("attempts", [])]
        r = d.get("result")
        if r is not None:
            job._result = result_from_dict(r)
        return job
