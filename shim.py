"""OpenAI-compatible chat shim — the swappable solver **workbench** (WORKBENCH_PLAN Phase B).

Our solvers are *loops* / *votes*, not chat models, so we can't point Open WebUI straight at a
model: it would show un-executed code, or run a single greedy pass with no checking. This shim
wraps the engine behind `/v1/chat/completions`, exposing the **lineup × rung** as selectable
"models" so swapping the Open WebUI dropdown is climbing the trust ladder (PLAN.md §1):

  …-cot     rung 1  — one streamed chat pass; read the last \\boxed{}.
  …-verify  rung 2  — cot + a self-check pass; the UI shows ✓ confirmed / ⚠ corrected.
  …-deep    rung 2′ — maj@k (k seeded chains); the UI shows the voted answer + agreement m/k.
  …-tir     incumbent — the sync TIR specialist (kernel-executed ```python), bridged async.

The picker is `contenders.jsonl` slugified (override `WORKBENCH_LINEUP`); maj@k-deep reuses
`eval.solve_graded_async` (don't re-implement voting). One renderer (`_render_pieces`) drives
every rung, dispatching only on the envelope `type` (streaming.py): model prose streams through
the FOIM delimiter buffer + `normalize_delimiters` (no half-open `\\boxed{`/`$…$`; `\(…\)`
renders), the hidden reasoning trace streams on a separate (collapsible) channel, tool output is
fenced, and a footer shows the rung · time · tokens · est-$.

Run:  direnv exec . uv run uvicorn shim:app --port 8000
Then point Open WebUI at  http://localhost:8000/v1  (see OPEN_WEBUI.md).
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse

from eval import solve_graded_async              # maj@k voting — reuse, don't re-implement
from solver_loop import solve_stream, Kernel      # the sync TIR specialist (bridged async)
from strategies import generalist_stream          # async cot / self_verify
from streaming import LatexSafeBuffer, normalize_delimiters, ev

_HERE = Path(__file__).resolve().parent
LINEUP_PATH = os.environ.get("WORKBENCH_LINEUP", str(_HERE / "contenders.jsonl"))
PRICES_PATH = os.environ.get("WORKBENCH_PRICES", str(_HERE / "prices.json"))

GENERALIST = {"cot", "self_verify"}               # streamable generalist rungs (need a model id)
DEEP_K = int(os.environ.get("WORKBENCH_DEEP_K", "5"))         # samples for the maj@k-deep rung
STREAM_MAX_TOKENS = int(os.environ.get("WORKBENCH_MAX_TOKENS", "16384"))  # thinking shares this

app = FastAPI(title="pudding solver workbench")


# --- the picker: lineup → (model × rung) -----------------------------------
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-")


def _load_lineup(path: str) -> list[dict]:
    """Parse the JSONL lineup (same convention as audition.py: '#'/blank lines ignored)."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(json.loads(line))
    return rows


def _build_models(rows: list[dict]) -> dict[str, dict]:
    """lineup rows → MODELS: slug → {provider, model, strategy, label, mode, k}.

    Each row becomes its rung (id = explicit "id" or slug(label); the label carries · CoT /
    · self-verify / · TIR). For every distinct generalist (provider, model) we also synthesize a
    maj@k `…-deep` sibling (WORKBENCH_PLAN §3.4–5). tir_fence rows route through the sync bridge.
    """
    models: dict[str, dict] = {}
    deep: dict[str, dict] = {}
    seen_deep: set = set()
    for r in rows:
        provider, model = r.get("provider"), r["model"]
        strategy = r.get("strategy", "cot")
        label = r.get("label") or f"{model} · {strategy}"
        slug = r.get("id") or _slug(label)
        mode = "tir" if strategy == "tir_fence" else "stream"
        models[slug] = {"provider": provider, "model": model, "strategy": strategy,
                        "label": label, "mode": mode, "k": None}
        if strategy in GENERALIST and (provider, model) not in seen_deep:
            seen_deep.add((provider, model))
            disp = label.split("·")[0].strip() or model      # model name minus the rung suffix
            deep[_slug(disp) + "-deep"] = {
                "provider": provider, "model": model, "strategy": "cot",
                "label": f"{disp} · maj@{DEEP_K}", "mode": "deep", "k": DEEP_K}
    models.update(deep)                                       # deep rungs after their base rungs
    return models


MODELS = _build_models(_load_lineup(LINEUP_PATH))
DEFAULT_MODEL = next(iter(MODELS), "")


# --- cost footer -----------------------------------------------------------
def _load_prices(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}   # skip the "_comment" string


PRICES = _load_prices(PRICES_PATH)


def _est_cost(model: str, tokens: int):
    """Estimated $ for `tokens` output at `model`'s price, or None if unknown/free."""
    out = (PRICES.get(model) or {}).get("out")
    return tokens * out / 1e6 if out and out > 0 else None


@app.get("/v1/models")
def list_models():
    """The lineup × rung, as selectable models. Swapping the dropdown climbs the trust ladder."""
    return {"object": "list", "data": [
        {"id": slug, "object": "model", "owned_by": "pudding", "name": e["label"]}
        for slug, e in MODELS.items()
    ]}


# --- engine dispatch -------------------------------------------------------
async def _aiter_sync(make_gen):
    """Bridge a *sync* event generator (the TIR solve_stream) into an async iterator: run it in a
    worker thread, hand events back through an asyncio.Queue. Lets the blocking specialist share
    the async picker without a thread-per-token (WORKBENCH_PLAN §3.8)."""
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    DONE = object()

    def worker():
        try:
            for e in make_gen():
                loop.call_soon_threadsafe(q.put_nowait, e)
        except Exception as ex:  # noqa: BLE001 — surface as an event, never hang the consumer
            loop.call_soon_threadsafe(q.put_nowait, ev("error", message=f"{type(ex).__name__}: {ex}"))
        finally:
            loop.call_soon_threadsafe(q.put_nowait, DONE)

    task = loop.run_in_executor(None, worker)
    try:
        while True:
            e = await q.get()
            if e is DONE:
                break
            yield e
    finally:
        await task


async def stream_events(problem: str, entry: dict):
    """Dispatch one solve by the picker entry's `mode`, yielding the shared envelope (streaming.py).

    stream → cot/self_verify (async generalist); deep → maj@k via solve_graded_async (status, then
    a final_answer carrying agreement); tir → the sync specialist bridged async. Unknown ids
    surface an `error` event so the UI never hangs.
    """
    mode = entry.get("mode")
    provider, model, strategy = entry.get("provider"), entry.get("model"), entry.get("strategy")
    if mode == "stream":
        async for e in generalist_stream(problem, strategy=strategy, provider=provider,
                                         model=model, max_tokens=STREAM_MAX_TOKENS):
            yield e
    elif mode == "deep":
        k = entry["k"]
        yield ev("status", text=f"⏳ running {k} samples (maj@{k})…")
        t0 = time.perf_counter()
        res = await solve_graded_async(problem, k, model, strategy="cot", provider=provider,
                                       max_tokens=STREAM_MAX_TOKENS)
        if res.get("pred") is None and res.get("error"):
            yield ev("error", message=res["error"])
            return
        yield ev("final_answer", boxed=res.get("pred"), agreement=res.get("agreement"), k=k,
                 completion_tokens=res.get("tokens") or 0,
                 elapsed_s=round(time.perf_counter() - t0, 1),
                 truncated=bool(res.get("truncated")))
    elif mode == "tir":
        def make_gen():
            kern = Kernel()
            try:
                yield from solve_stream(problem, executor=kern, model=model, provider=provider,
                                        strategy="tir_fence", stream=True)
            finally:
                try:
                    kern.km.shutdown_kernel(now=True)
                except Exception:
                    pass
        async for e in _aiter_sync(make_gen):
            yield e
    else:
        yield ev("error", message=f"unknown model {entry.get('label')!r}; pick one from /v1/models")


# --- rendering: envelope → (channel, text) pieces --------------------------
def _verdict(e: dict) -> str:
    """Trust annotation rendered just before the footer, from the final_answer's optional fields:
    maj@k agreement (the trustworthy surface — WORKBENCH_PLAN §2.5), or self_verify's pass-1 vs
    final boxed (experimental — the single-pass critic can over-correct)."""
    boxed = e.get("boxed")
    if "agreement" in e and e.get("k"):
        k = e["k"]
        if not boxed:
            return "\n\n⚠ no agreement — the samples produced no boxed answer.\n"
        m = round((e.get("agreement") or 0) * k)
        return f"\n\n**\\({boxed}\\)**  ·  agreement {m}/{k}\n"
    if "candidate_boxed" in e:
        cand = e.get("candidate_boxed")
        if boxed and cand and boxed.strip() == cand.strip():
            return f"\n\n✓ self-checked: confirmed \\({boxed}\\)\n"
        if boxed and cand and boxed.strip() != cand.strip():
            return f"\n\n⚠ self-check corrected \\({cand}\\) → \\({boxed}\\)\n"
    return ""


def _footer(e: dict, entry: dict) -> str:
    """`— {label} · ⏱ {s}s · {tok} tok · ~${est}` (· ⚠ truncated). The est-$ appears when the
    model's output price is known (prices.json)."""
    parts = [f"— {entry['label']}"]
    if e.get("elapsed_s") is not None:
        parts.append(f"⏱ {e['elapsed_s']}s")
    toks = e.get("completion_tokens") or 0
    if toks:
        parts.append(f"{toks} tok")
        cost = _est_cost(entry.get("model", ""), toks)
        if cost is not None:
            parts.append(f"~${cost:.4f}")
    footer = "\n\n" + " · ".join(parts)
    if e.get("truncated"):
        footer += " · ⚠ truncated (raise effort)"
    return footer


async def _render_pieces(problem: str, entry: dict):
    """Yield (channel, text) pieces for one solve — channel ∈ {"content", "reasoning"}.

    content  = FOIM-buffered, delimiter-normalized prose + fenced tool output + the trust verdict
               and footer; reasoning = the model's hidden thinking trace (a separate, collapsible
               channel). Driven only by envelope `type`, so every rung/model renders the same way.
    Shared by the streaming and non-streaming paths.
    """
    buf = LatexSafeBuffer()        # visible content
    tbuf = LatexSafeBuffer()       # hidden thinking
    async for e in stream_events(problem, entry):
        t = e["type"]
        if t == "reasoning_delta":
            safe = buf.feed(e["text"])
            if safe:
                yield "content", normalize_delimiters(safe)
        elif t == "thinking_delta":
            safe = tbuf.feed(e["text"])
            if safe:
                yield "reasoning", normalize_delimiters(safe)
        elif t == "status":
            yield "content", f"_{e['text']}_\n"
        elif t == "code":
            pass                   # the model already wrote the ```python block in its prose
        elif t == "tool_result":
            tail = buf.flush()     # flush held prose before the injected output
            if tail:
                yield "content", normalize_delimiters(tail)
            yield "content", f"\n```output\n{e['output']}\n```\n"
        elif t == "final_answer":
            rtail = tbuf.flush()
            if rtail:
                yield "reasoning", normalize_delimiters(rtail)
            tail = buf.flush()
            if tail:
                yield "content", normalize_delimiters(tail)
            verdict = _verdict(e)
            if verdict:
                yield "content", verdict
            yield "content", _footer(e, entry)
        elif t == "error":
            tail = buf.flush()
            if tail:
                yield "content", normalize_delimiters(tail)
            yield "content", f"\n\n**error:** {e['message']}"
    rtail = tbuf.flush()           # safety: stream ended without a final_answer
    if rtail:
        yield "reasoning", normalize_delimiters(rtail)


# --- OpenAI-compatible chat endpoint ---------------------------------------
def _chunk(*, model, content=None, reasoning=None, role=False, finish=None):
    delta = {}
    if role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning      # collapsible "thinking" channel (OWUI)
    body = {
        "id": "chatcmpl-pudding", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    messages = body.get("messages", [])
    model = body.get("model") or DEFAULT_MODEL
    stream = body.get("stream", False)
    # The last user turn is the problem (each message = a fresh problem; the solver is stateless).
    problem = next((m.get("content", "") for m in reversed(messages)
                    if m.get("role") == "user"), "")
    entry = MODELS.get(model, {"provider": None, "model": model, "strategy": "cot",
                               "label": model, "mode": "unknown", "k": None})

    if stream:
        async def sse():
            yield _chunk(model=model, role=True)
            try:
                async for channel, piece in _render_pieces(problem, entry):
                    if channel == "reasoning":
                        yield _chunk(model=model, reasoning=piece)
                    else:
                        yield _chunk(model=model, content=piece)
            except Exception as ex:  # noqa: BLE001 — never leave the SSE stream half-open
                yield _chunk(model=model, content=f"\n\n**error:** {type(ex).__name__}: {ex}")
            yield _chunk(model=model, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    content_parts, reasoning_parts = [], []
    async for channel, piece in _render_pieces(problem, entry):
        (reasoning_parts if channel == "reasoning" else content_parts).append(piece)
    msg = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    return JSONResponse({
        "id": "chatcmpl-pudding", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
