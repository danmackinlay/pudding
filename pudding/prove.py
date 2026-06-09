"""The prover engine (Track 2) — generate a Lean proof, let the unfakeable compiler judge it.

A **reducer + engine swap on the existing `Job`** (FORK.md, STUDIO_PLAN decision #7), NOT a new
job layer: `solver_loop.solve_one_async` dispatches `strategy=="prove"` to `prove_one_async`, and
`jobs._reduce` dispatches `reduce_proofs`. Everything else — fan-out, persistence, streaming
envelope, `Job` handle — is reused unchanged.

Engine: a generalist closes the `sorry` (the 2026 surprise is that generalists are competitive
provers — Claude Opus one-pass Lean), the Kimina Lean server (`verifier/modal_verifier.py`) stamps
✓ only on a *closed* proof, and on rejection we feed the compiler errors back (Goedel-V2-style
self-correction). The "vote" is **Pass@k**: the shortest candidate that compiles wins — cheap,
because the compiler can't be fooled.

The honest render is the §3 conditional string: "proof verified · N/k compiled · faithfulness …".
Q1 only fills the `type-checks ✓` rung; the numeric / back-translation / negation rungs of the
**faithfulness gate** land in Q3 (the actual USP). A green ✓ on an *unfaithful* statement is worse
than no proof — so this never hides that faithfulness is, for now, unchecked.
"""
from __future__ import annotations

import asyncio
import os
import re
import time

from providers import make_async_client
from strategies import stream_chat        # one streamed chat call → ('delta'|'reasoning'|'done')

# OpenRouter slug for the generalist that closes the sorry. Override per-machine without editing
# code: PUDDING_PROVER_MODEL / PUDDING_PROVER_ROUNDS. (No Novita key here → generalist route.)
DEFAULT_PROVER_MODEL = os.environ.get("PUDDING_PROVER_MODEL", "anthropic/claude-opus-4.5")
DEFAULT_MAX_ROUNDS = int(os.environ.get("PUDDING_PROVER_ROUNDS", "1"))   # self-correction rounds
DEFAULT_MAX_TOKENS = 16384            # proof + plan run long (PROVER_RESEARCH_ADDENDUM §3)
DEFAULT_VERIFY_TIMEOUT = 120          # seconds per /api/check

PROVE_SYS = ("You are an expert in Lean 4 and Mathlib. You write complete, compiling formal "
             "proofs and never leave a `sorry`.")
PROVE_USER = """Complete the following Lean 4 theorem by replacing `sorry` with a correct, \
compiling proof.

```lean4
{statement}
```

Give a brief proof plan, then output the COMPLETE Lean 4 file — the imports, the theorem, and \
your proof — in a SINGLE ```lean4 code block. Use Mathlib. Leave no `sorry` and no `admit`."""

# The last Lean code fence. Lenient on the language tag — models drift between ```lean4, ```lean,
# and a bare ``` — so accept any fence and prefer the last block that actually looks like Lean.
_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def extract_proof(text: str) -> str | None:
    blocks = [b.rstrip("` \n") for b in _FENCE.findall(text or "")]
    leanish = [b for b in blocks if ("theorem" in b or "lemma" in b or "import " in b)]
    cands = leanish or blocks
    return cands[-1].strip() if cands else None


# --- anti-laundering: the model must prove the statement we PASTED, not a weaker rewrite -----
# The generalist emits a whole file, so it could silently alter the theorem (drop a hypothesis,
# weaken a bound) and prove that instead — a green ✓ on the wrong statement, the exact failure this
# track refuses. Q1 guard: the theorem *signature* we handed over must appear verbatim (whitespace-
# normalised) in the candidate. (The richer faithfulness gate — numeric / back-translation / negation
# — is Q3; this only enforces "prove THIS formal statement, unchanged".)
def _normspace(s: str) -> str:
    return "".join(s.split())        # drop ALL whitespace — robust to reformatting, still catches
                                     # a dropped hypothesis / changed bound / renamed theorem


def _signature(statement: str) -> str:
    """The theorem signature: from `theorem`/`lemma`/`example` up to the proof assignment `:=`."""
    head = statement.split(":=", 1)[0]
    for kw in ("theorem ", "lemma ", "example "):
        i = head.find(kw)
        if i != -1:
            return _normspace(head[i:])
    return _normspace(head)


def _preserves_signature(statement: str, proof: str) -> bool:
    return _signature(statement) in _normspace(proof)


def _retry_user(errors: list) -> str:
    lines = "\n".join(str(e.get("data") or e.get("text") or e) for e in (errors or [])[:8])
    return ("The Lean compiler REJECTED that proof:\n\n" + lines +
            "\n\nFix it and output the complete corrected Lean 4 file in one ```lean4 block.")


# --- the verifier handle ---------------------------------------------------
_VERIFIER = None


def _get_verifier():
    """The deployed Modal Lean verifier (`modal deploy verifier/modal_verifier.py` once).

    Cached module-wide so the k Pass@k lanes share one warm Lean server. Tests inject a fake
    via the `verifier=` arg instead of reaching Modal.
    """
    global _VERIFIER
    if _VERIFIER is None:
        import modal
        _VERIFIER = modal.Cls.from_name("pudding-verifier", "LeanVerifier")()
    return _VERIFIER


async def _one_call(client, model, messages, temperature, seed, max_tokens):
    """Run one streamed chat call; accumulate visible text, hidden reasoning, and the info dict."""
    text = reasoning = ""
    info: dict = {}
    async for kind, payload in stream_chat(client, model, messages, temperature, max_tokens, seed):
        if kind == "delta":
            text += payload
        elif kind == "reasoning":
            reasoning += payload
        else:
            info = payload
    return text, reasoning, info


async def prove_one_async(statement: str, *, model: str | None = None, provider: str | None = "openrouter",
                          temperature: float = 0.0, seed: int | None = None,
                          max_tokens: int = DEFAULT_MAX_TOKENS, max_rounds: int | None = None,
                          verifier=None, client=None, verify_timeout: int = DEFAULT_VERIFY_TIMEOUT) -> dict:
    """One Pass@k lane: generate → verify → (self-correct) → a `Job`-`Attempt`-shaped dict.

    Returns the solver-lane contract — {boxed, transcript, completion_tokens, truncated, elapsed_s,
    ttft_s, decode_tok_s, error, thinking} — so `Job._collect` builds the `Attempt` unchanged.
    `boxed` is the **verified proof text** (or `None` if no candidate compiled), which is exactly
    what `reduce_proofs` ranks. Prover extras {ok, proof, rounds, lean_errors} ride along for the
    gallery/dossier (ignored by the current `Attempt` record). Never raises — failures land in
    `error`, exactly like `solve_one_async`.
    """
    model = model or DEFAULT_PROVER_MODEL
    max_rounds = DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds
    verifier = verifier or _get_verifier()
    client = client or make_async_client(provider)
    t0 = time.perf_counter()
    out = {"boxed": None, "transcript": "", "completion_tokens": 0, "truncated": False,
           "elapsed_s": 0.0, "ttft_s": None, "decode_tok_s": None, "error": None, "thinking": "",
           "ok": False, "proof": None, "rounds": 0, "lean_errors": []}
    msgs = [{"role": "system", "content": PROVE_SYS},
            {"role": "user", "content": PROVE_USER.format(statement=statement)}]
    parts, last_errors, ttft = [], [], None
    try:
        for round_i in range(max_rounds + 1):
            out["rounds"] = round_i
            text, reasoning, info = await _one_call(client, model, msgs, temperature, seed, max_tokens)
            out["completion_tokens"] += info.get("completion_tokens") or 0
            out["truncated"] = out["truncated"] or info.get("finish_reason") == "length"
            ttft = ttft if ttft is not None else info.get("ttft_s")
            out["thinking"] += reasoning
            parts.append(text)

            proof = extract_proof(text)
            if proof is None:                       # no fence — nudge once and retry if rounds remain
                last_errors = [{"data": "model output had no ```lean4 fence"}]
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Output the complete Lean 4 proof in a "
                                                      "single ```lean4 code block."}]
                continue

            if not _preserves_signature(statement, proof):    # anti-laundering: prove the ORIGINAL,
                last_errors = [{"data": "the candidate altered the theorem statement; the signature "
                                        "must match the one given, exactly"}]
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": _retry_user(last_errors) +
                          " Keep the `theorem` signature EXACTLY as given; change only the proof."}]
                continue

            # The model is asked to include imports; if it dropped them, prepend the std header.
            add_header = "import " not in proof
            verdict = await _check(verifier, proof, verify_timeout, add_header)
            if verdict.get("ok"):
                out.update(ok=True, proof=proof, boxed=proof, lean_errors=[])
                break
            last_errors = verdict.get("errors") or [{"data": "rejected (no error detail)"}]
            msgs += [{"role": "assistant", "content": text},
                     {"role": "user", "content": _retry_user(last_errors)}]
        out["lean_errors"] = [] if out["ok"] else last_errors
    except Exception as e:        # noqa: BLE001 — surface, never crash the fan-out
        out["error"] = f"{type(e).__name__}: {e}"
    el = time.perf_counter() - t0
    out["elapsed_s"] = round(el, 2)
    out["ttft_s"] = ttft
    out["decode_tok_s"] = (round(out["completion_tokens"] / el, 1)
                           if el > 0 and out["completion_tokens"] else None)
    out["transcript"] = _transcript(statement, parts, out)
    return out


async def _check(verifier, proof: str, timeout: int, add_header: bool) -> dict:
    """Call the verifier's `run` whether it's a Modal handle (`.run.remote.aio`) or a plain object."""
    run = verifier.run
    remote = getattr(run, "remote", None)
    if remote is not None:                  # Modal @method → async remote call
        return await remote.aio(proof, timeout=timeout, add_header=add_header)
    res = run(proof, timeout=timeout, add_header=add_header)        # injected fake (sync or async)
    return await res if asyncio.iscoroutine(res) else res


def _transcript(statement: str, parts: list[str], out: dict) -> str:
    body = parts[-1] if parts else ""
    if out["ok"]:
        head = f"✓ verified (round {out['rounds']}, {len((out['proof'] or '').splitlines())} lines)"
    elif out["error"]:
        head = f"engine error: {out['error']}"
    else:
        errs = "; ".join(str(e.get("data") or e)[:120] for e in (out["lean_errors"] or [])[:3])
        head = f"✗ no candidate compiled (rounds={out['rounds']}): {errs}"
    return f"**{head}**\n\n{body}"


# --- the reducer swap: Pass@k = the shortest candidate that compiles -------
def reduce_proofs(attempts, spec) -> "object":
    """Prover reducer (`jobs._reduce` dispatches here for strategy=='prove'). The winner is the
    SHORTEST verified proof (cheap to read, likely cleanest); 'agreement' is the Pass@k rate
    N_compiled/k. Builds the same `Result` record the solver uses, so persistence/reuse/pin
    all work unchanged."""
    from .jobs import Cluster, Result               # lazy: avoid an import cycle with jobs._reduce
    from .artifacts import est_cost_total

    compiled = sorted((a for a in attempts if a.boxed), key=lambda a: len(a.boxed or ""))
    n_total = len(attempts)
    n_ok = len(compiled)
    winner = compiled[0] if compiled else None
    clusters = ([Cluster(answer=winner.boxed, key="verified", count=n_ok,
                         models=sorted({a.model for a in compiled}))]
                if winner else [])
    r = Result(answer=(winner.boxed if winner else None), count=n_ok, n_answered=n_ok,
               n_total=n_total, agreement=(n_ok / n_total if n_total else 0.0),
               cross_model=(len({a.model for a in compiled}) > 1), clusters=clusters,
               attempts=attempts, tokens=sum(a.tokens for a in attempts),
               cost=est_cost_total(attempts), k=spec["k"], models=spec["model_names"],
               markdown="", provenance=_prove_prov(spec, attempts))
    r.markdown = _prove_markdown(r, spec)
    return r


def _prove_prov(spec, attempts) -> dict:
    return {"problem": spec["problem"], "k": spec["k"], "models": spec["model_names"],
            "strategy": "prove", "created": time.time(),
            "attempts": [{"model": a.model, "seed": a.seed,
                          "boxed": (a.boxed[:80] if a.boxed else None),   # truncate the proof in prov
                          "tokens": a.tokens, "error": a.error} for a in attempts]}


def _prove_markdown(r, spec) -> str:
    stmt = spec.get("problem", "")
    if r.answer:
        lines = len(r.answer.splitlines())
        return (f"**proof verified** · {r.count}/{r.n_total} candidates compiled\n\n"
                f"_statement:_\n```lean4\n{stmt}\n```\n\n"
                f"_shortest verified proof ({lines} lines):_\n```lean4\n{r.answer}\n```\n\n"
                f"> **faithfulness:** type-checks ✓ · numeric — · back-translation — · negation — "
                f"_(the gate lands in Q3; a ✓ here means Lean accepted the proof, NOT that the "
                f"statement faithfully captures the intent)_")
    engine_err = next((a.error for a in r.attempts if a.error), None)
    tail = f"\n\n_last engine error:_ `{engine_err}`" if engine_err else ""
    return (f"**no candidate compiled in k={r.n_total}** — an honest miss, not a laundered ✓.\n\n"
            f"_statement:_\n```lean4\n{stmt}\n```" + tail)


# --- the public verb: pudding.prove(statement, k=…) → a Job ----------------
def prove(statement: str, *, k: int = 8, model: str | None = None, provider: str | None = "openrouter",
          max_tokens: int | None = None, concurrency: int = 4, timeout: float | None = None,
          max_rounds: int | None = None, on_event=None, sem=None):
    """Pass@k a hand-written Lean `theorem … := by sorry` → a `Job` of verified-or-not proofs.

        job = pudding.prove("import Mathlib\\n\\ntheorem t : 1 + 1 = 2 := by sorry", k=8)
        res = await job          # async   |   res = pudding.prove(...).result()   # sync
        print(res.markdown)      # honest: "proof verified · N/k compiled" or "no candidate compiled"

    Mirrors `pudding.solve` (the identical `Job`/widget surface) with `strategy='prove'` and a
    generalist prover slug. `concurrency` is bounded by ONE warm Lean server's REPL pool.
    """
    import uuid
    from . import lineup, store
    from .jobs import Job, Lane

    model = model or DEFAULT_PROVER_MODEL
    prov, model_id = lineup.resolve(model, provider)
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    temperature = 0.0 if k == 1 else 0.6          # k==1 → greedy; k>1 → diverge the Pass@k chains
    lanes = [Lane(name=model, provider=prov, model=model_id, strategy="prove", seed=s,
                  temperature=temperature) for s in range(k)]
    spec = {"problem": statement, "k": k, "model_names": [model], "strategy": "prove",
            "max_tokens": max_tokens, "concurrency": concurrency, "timeout": timeout,
            "temperature": temperature,
            "max_rounds": DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds}
    job = Job(uuid.uuid4().hex[:12], spec, lanes, on_event=on_event, sem=sem)
    store.write(job.id, job.to_dict())
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        job._schedule(loop)
    return job
