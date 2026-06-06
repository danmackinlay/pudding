"""The generative loop — conjecture → falsify → (survivors worth proving) (STUDIO_PLAN P3).

The discipline (§0): **AI proposes abundantly, the cheap oracle disposes ruthlessly, the human
curates the survivors.** Generation is expensive-ish but the *filter is nearly free*, so we exploit
the asymmetry: an LLM proposes a flock of falsifiable conjectures, each carrying a Python
falsification harness; a cheap oracle runs every harness in parallel and kills the false ones in
milliseconds; the survivors earn the expensive `solve`/`prove` fan-out.

Two honesty rails:
  * **The oracle decides, not the prose.** A conjecture's fate is the *return value of its
    `counterexample()` search*, never the model's confidence. The prompt says: propose, don't assert.
  * **Surviving ≠ proven.** A survivor is merely *not refuted by this search* — a candidate worth
    proving, not a theorem. The artifact says so.

Headless, zero frontend deps (decision #1/#9): async verbs + opt-in `on_event`; a static markdown
artifact + a `view_model` for the studio. The falsification oracle is an **isolated subprocess**
(`python -I`) — genuinely parallel, with a real timeout/kill (a runaway search can't hang the loop)
and crash isolation, and sympy/numpy resolve from the project venv.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field

from streaming import ev
from . import lineup

# --- the proposal prompt (propose, don't assert; ship a falsifier) ---------
CONJECTURE_SYS = (
    "You are a mathematical conjecture generator. Given some context, propose distinct, "
    "non-obvious, FALSIFIABLE conjectures — precise claims that *might* be true. You PROPOSE; you "
    "do NOT assert. A separate computational oracle — not your prose — decides each one's fate, so "
    "every conjecture must ship with a way to try to break it.\n\n"
    "For EACH conjecture give exactly these fields:\n"
    "  • statement — the claim in clear prose + LaTeX (inline $...$).\n"
    "  • rationale — one sentence on why it's plausible.\n"
    "  • check — a self-contained Python snippet defining a function `counterexample()` that "
    "SEARCHES a sensible finite domain for a counterexample and RETURNS it (any short value, tuple, "
    "or string) if it finds one, or RETURNS None if the search finds none. Make the search genuinely "
    "try to break the claim (sweep many cases). It must be fast (well under a second), use only the "
    "standard library / sympy / numpy, and do NO I/O, printing, or network.\n\n"
    "Return ONLY a JSON array of objects with keys statement, rationale, check, inside a ```json "
    "fence. The check is the sole arbiter — write the statement as a claim to be tested, not a proof."
)


def _user_prompt(context: str, n: int) -> str:
    return (f"Context:\n{context.strip()}\n\n"
            f"Propose {n} distinct, falsifiable conjectures in the specified JSON format. "
            f"Prefer claims your `counterexample()` can actually probe over a finite domain.")


# --- records ---------------------------------------------------------------
@dataclass
class Conjecture:
    id: str                       # short stable id within the flock (c0, c1, …)
    statement: str                # the claim — prose + LaTeX
    check: str                    # Python harness defining counterexample()
    origin: str                   # which model proposed it (provenance)
    rationale: str = ""
    status: str = "proposed"      # proposed | survives | refuted | error
    witness: str | None = None    # the counterexample (repr), when refuted
    detail: str = ""              # oracle note / error message


@dataclass
class Flock:
    """A generated flock + (after falsify) its fates. The survivors are the product: claims the
    cheap oracle could not break, hence worth the expensive solve/prove fan-out."""
    context: str
    conjectures: list[Conjecture]
    models: list[str]
    provenance: dict = field(default_factory=dict)
    markdown: str = ""

    @property
    def survivors(self) -> list[Conjecture]:
        return [c for c in self.conjectures if c.status == "survives"]

    @property
    def refuted(self) -> list[Conjecture]:
        return [c for c in self.conjectures if c.status == "refuted"]

    @property
    def unchecked(self) -> list[Conjecture]:    # proposed-but-not-yet / harness errored
        return [c for c in self.conjectures if c.status in ("proposed", "error")]

    def counts(self) -> dict:
        out = {"proposed": 0, "survives": 0, "refuted": 0, "error": 0}
        for c in self.conjectures:
            out[c.status] = out.get(c.status, 0) + 1
        return out


# --- (de)serialization -----------------------------------------------------
def flock_to_dict(f: Flock) -> dict:
    return asdict(f)


def flock_from_dict(d: dict) -> Flock:
    d = dict(d)
    d["conjectures"] = [Conjecture(**c) for c in d.get("conjectures", [])]
    return Flock(**d)


# --- generation (the LLM proposal seam — monkeypatched in tests) -----------
async def _propose_async(context: str, *, n: int, model: str, provider: str | None,
                         temperature: float, seed: int | None, max_tokens: int) -> str:
    """One model call → raw text (expected to contain a ```json array). Reuses the audition's
    streamed chat (strategies._chat) so TTFT/usage plumbing and the reasoning-channel fallback
    are shared. Tests replace this with a scripted fake — the network-free seam."""
    from providers import make_async_client
    from strategies import stream_chat
    client = make_async_client(provider)
    msgs = [{"role": "system", "content": CONJECTURE_SYS},
            {"role": "user", "content": _user_prompt(context, n)}]
    text, info = "", {}
    async for kind, payload in stream_chat(client, model, msgs, temperature, max_tokens, seed):
        if kind == "delta":
            text += payload
        elif kind == "done":
            info = payload
    return text or info.get("text") or info.get("reasoning") or ""   # thinking-only models fallback


def _parse_candidates(text: str) -> list[dict]:
    """Tolerant extraction of the candidate dicts from model output: a clean ```json fence or
    outermost [...] first, then — crucially — **per-object salvage** so a truncated array (a
    reasoning model that ran out of budget mid-JSON) still yields its complete conjectures rather
    than nothing. Never raises — unparseable output → []."""
    if not text:
        return []
    for blob in (_fenced_json(text), _bracket_span(text)):
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except Exception:
            continue
        if isinstance(data, dict):
            data = data.get("conjectures") or data.get("candidates") or [data]
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict)]
    # salvage: scan for complete top-level {...} objects (survives an unclosed fence / cut-off array)
    out = []
    for chunk in _brace_objects(text):
        try:
            d = json.loads(chunk)
        except Exception:
            continue
        if isinstance(d, dict) and ("statement" in d or "check" in d):
            out.append(d)
    return out


def _fenced_json(text: str) -> str | None:
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _bracket_span(text: str) -> str | None:
    i, j = text.find("["), text.rfind("]")
    return text[i:j + 1] if (i != -1 and j > i) else None


def _brace_objects(text: str):
    """Yield each complete top-level `{...}` substring (JSON-string-aware, double-quote only). A
    trailing incomplete object never balances, so it's silently dropped — exactly what we want."""
    depth = start = 0
    started = in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start, started = i, True
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and started:
                yield text[start:i + 1]
                started = False


async def conjecture_async(context: str, *, n: int = 8, models: list[str] | None = None,
                           provider: str | None = None, temperature: float = 0.8,
                           max_tokens: int = 12288, on_event=None) -> Flock:
    """Propose `n` falsifiable conjectures from `context`, split across `models` (provenance-tagged).
    Returns a `Flock` of `proposed` conjectures — run `falsify_async` next. Cheap; await it."""
    names = list(models) if models else lineup.default_models()[:1]
    if not names:
        raise ValueError("no models — pass models=[...] or populate contenders.jsonl")
    share = [n // len(names) + (1 if i < n % len(names) else 0) for i in range(len(names))]
    conjs: list[Conjecture] = []
    for name, cnt in zip(names, share):
        if cnt <= 0:
            continue
        prov, model_id = lineup.resolve(name, provider)
        text = await _propose_async(context, n=cnt, model=model_id, provider=prov,
                                    temperature=temperature, seed=len(conjs), max_tokens=max_tokens)
        for cand in _parse_candidates(text)[:cnt]:
            c = Conjecture(id=f"c{len(conjs)}", origin=name,
                           statement=str(cand.get("statement", "")).strip(),
                           rationale=str(cand.get("rationale", "")).strip(),
                           check=str(cand.get("check", "")))
            conjs.append(c)
            _emit(on_event, ev("conjecture", id=c.id, origin=name, statement=c.statement[:140]))
    flock = Flock(context=context, conjectures=conjs, models=names,
                  provenance={"context": context[:500], "n": n, "models": names,
                              "created": time.time()})
    flock.markdown = flock_markdown(flock)
    return flock


# --- the falsification oracle (cheap, parallel, killable) ------------------
# A driver that exec()s the model's harness in a clean namespace and prints one sentinel line.
# Wrapping everything in try/except guarantees a sentinel (so a broken harness reads as an honest
# 'error', not a silent survive); a runaway search is killed by the wall-clock timeout instead.
_DRIVER = (
    "import sys, io, contextlib\n"
    "_SRC = {src!r}\n"
    "_ns = {{}}\n"
    "_verdict = sys.stdout\n"                       # the ONE real channel the parser reads
    "with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):\n"
    "    try:\n"                                     # the harness's own prints can't reach _verdict,
    "        exec(_SRC, _ns)\n"                      # so they can't spoof the sentinel (S1)
    "        if 'counterexample' not in _ns:\n"
    "            raise NameError('harness did not define counterexample()')\n"
    "        _r = _ns['counterexample']()\n"
    "        _out = '__PUDDING_OK__' + repr(_r)[:400]\n"
    "    except BaseException as _e:\n"
    "        _out = '__PUDDING_ERR__' + type(_e).__name__ + ': ' + str(_e)[:400]\n"
    "_verdict.write(_out)\n"
)


async def _run_check(check_code: str, *, timeout: float) -> tuple[str, str | None, str]:
    """Run one harness in an isolated subprocess → (status, witness, detail).
    `counterexample()` returning None ⇒ survives; any other value ⇒ refuted (that value is the
    witness); a crash/missing-harness ⇒ error; exceeding `timeout` ⇒ error (killed)."""
    driver = _DRIVER.format(src=check_code)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", driver,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:                       # noqa: BLE001
        return ("error", None, f"could not spawn oracle: {e}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        await proc.wait()
        return ("error", None, f"timeout ({timeout:g}s) — the search was too slow")
    except asyncio.CancelledError:
        _kill(proc)
        await proc.wait()
        raise
    text = (out or b"").decode("utf-8", "replace")
    if "__PUDDING_OK__" in text:
        witness = text.split("__PUDDING_OK__", 1)[1].strip()
        if witness == "None":
            return ("survives", None, "no counterexample found in the search")
        return ("refuted", witness, "counterexample found by the oracle")
    if "__PUDDING_ERR__" in text:
        return ("error", None, text.split("__PUDDING_ERR__", 1)[1].strip())
    tail = (err or b"").decode("utf-8", "replace").strip().splitlines()
    return ("error", None, "harness crashed: " + (tail[-1] if tail else "no output"))


def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def falsify_async(flock: Flock, *, timeout: float = 8.0, concurrency: int = 8,
                        on_event=None) -> Flock:
    """Run every conjecture's harness through the oracle in parallel (≤ `concurrency` subprocesses),
    setting each one's status/witness. Mutates and returns the same flock (re-rendered). The cheap,
    ruthless half of the loop — most false conjectures die here for ~free."""
    sem = asyncio.Semaphore(concurrency)

    async def check(c: Conjecture) -> None:
        if not c.check.strip():
            c.status, c.detail = "error", "no falsification harness was provided"
        else:
            async with sem:
                c.status, c.witness, c.detail = await _run_check(c.check, timeout=timeout)
        _emit(on_event, ev("verdict", id=c.id, status=c.status, witness=c.witness))

    await asyncio.gather(*[check(c) for c in flock.conjectures])
    flock.markdown = flock_markdown(flock)
    return flock


async def discover_async(context: str, *, n: int = 8, models: list[str] | None = None,
                         provider: str | None = None, timeout: float = 8.0, concurrency: int = 8,
                         on_event=None) -> Flock:
    """The whole loop in one call: conjecture → falsify → a flock whose `.survivors` are the
    candidates worth proving. `asyncio.run(pudding.discover_async(...))` headless; await it live."""
    flock = await conjecture_async(context, n=n, models=models, provider=provider,
                                   on_event=on_event)
    return await falsify_async(flock, timeout=timeout, concurrency=concurrency, on_event=on_event)


# --- rendering (library owns the static view-model + markdown; decision #9) -
_BADGE = {"survives": "✅ survives", "refuted": "❌ refuted", "error": "⚠ unchecked",
          "proposed": "· proposed"}


def flock_markdown(flock: Flock) -> str:
    c = flock.counts()
    head = (f"### conjecture flock — {len(flock.conjectures)} proposed"
            f" · {c['survives']} survive · {c['refuted']} refuted"
            + (f" · {c['error']} unchecked" if c["error"] else ""))
    note = ("*proposed by " + ", ".join(flock.models) + "; the oracle decides, not the prose. "
            "**Surviving ≠ proven** — a survivor is only *not refuted by the search*, i.e. a "
            "candidate worth proving.*")
    rows = ["| # | status | conjecture | by |", "|---|---|---|---|"]
    for cj in flock.conjectures:
        tag = _BADGE.get(cj.status, cj.status)
        extra = (f" — counterexample `{cj.witness}`" if cj.status == "refuted" and cj.witness
                 else (f" — _{cj.detail}_" if cj.status == "error" else ""))
        stmt = cj.statement.replace("\n", " ").replace("|", "\\|")
        rows.append(f"| {cj.id} | {tag} | {stmt}{extra} | {cj.origin} |")
    surv = flock.survivors
    tail = ""
    if surv:
        tail = ("\n\n**candidates worth proving** (survived falsification — *not yet proven*):\n"
                + "\n".join(f"- {s.statement}" for s in surv))
    return f"{head}\n\n{note}\n\n" + "\n".join(rows) + tail


def flock_view_model(flock: Flock) -> dict:
    """The studio's data (decision #9): counts, per-conjecture rows, survivor statements, artifact.
    Interactive widgets stay in studio/; this stays frontend-neutral."""
    return {"context": flock.context, "models": flock.models, "counts": flock.counts(),
            "conjectures": [{"id": c.id, "statement": c.statement, "status": c.status,
                             "badge": _BADGE.get(c.status, c.status), "witness": c.witness,
                             "detail": c.detail, "origin": c.origin, "rationale": c.rationale,
                             "check": c.check} for c in flock.conjectures],
            "survivors": [c.statement for c in flock.survivors],
            "markdown": flock.markdown or flock_markdown(flock)}


# --- internals -------------------------------------------------------------
def _emit(sink, event: dict) -> None:
    if sink is not None:
        try:
            sink(event)
        except Exception:        # noqa: BLE001 — a consumer's sink must never break the loop
            pass
