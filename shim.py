"""OpenAI-compatible chat shim that runs the TIR solver loop — the bridge to a chat UI.

Our solver is a *loop* (generate → run code → splice → repeat), not a chat model, so we
can't point Open WebUI straight at the model: it would show un-executed code, or push
execution into OWUI's own interpreter and collapse the repo's three separable roles. This
shim wraps `solve_stream` (which owns the kernel) behind `/v1/chat/completions`, so any
OpenAI-compatible frontend gets the *executed* transcript. Single streamed chain only —
maj@k stays in eval.py/fanout.py (PLAN.md §1.5/§1.7).

Rendering: model prose streams through the FOIM delimiter buffer + `normalize_delimiters`
(so the frontend never sees a half-open `\\boxed{`/`$…$` and `\(…\)` renders); code/tool
results are wrapped as ```python / ```output fences; a footer shows time/tokens.

Run:  direnv exec . uv run uvicorn shim:app --port 8000
Then point Open WebUI at  http://localhost:8000/v1  (see OPEN_WEBUI.md).
"""
import json
import os
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse

from solver_loop import solve_stream, Kernel, MODEL
from streaming import LatexSafeBuffer, normalize_delimiters

MODEL_ID = "tir-solver"  # what the frontend shows / sends; the real model is SOLVER_MODEL
SOLVER_MODEL = os.environ.get("SOLVER_MODEL", MODEL)

app = FastAPI(title="pudding TIR shim")


@app.get("/v1/models")
def list_models():
    # A single streamed-chain solver. (A `tir-solver-deep` effort preset could be added
    # later — bigger budget, NOT maj@k, which lives in eval.py/fanout.py.)
    return {"object": "list", "data": [
        {"id": MODEL_ID, "object": "model", "owned_by": "pudding"},
    ]}


def _render_pieces(problem: str):
    """Yield human-facing content pieces for one solve: FOIM-buffered prose, fenced code &
    tool output, and a time/token footer. Shared by the streaming and non-streaming paths."""
    buf = LatexSafeBuffer()
    kern = Kernel()
    try:
        for e in solve_stream(problem, executor=kern, model=SOLVER_MODEL, stream=True):
            t = e["type"]
            if t == "reasoning_delta":
                safe = buf.feed(e["text"])
                if safe:
                    yield normalize_delimiters(safe)
            elif t == "code":
                pass  # the model already wrote the ```python block in its prose — don't dup it
            elif t == "tool_result":
                tail = buf.flush()                       # flush held prose before the injected output
                if tail:
                    yield normalize_delimiters(tail)
                yield f"\n```output\n{e['output']}\n```\n"
            elif t == "final_answer":
                tail = buf.flush()
                if tail:
                    yield normalize_delimiters(tail)
                footer = f"\n\n— ⏱ {e['elapsed_s']}s · {e['completion_tokens']} tok"
                if e["truncated"]:
                    footer += " · ⚠ truncated (raise effort)"
                yield footer
            elif t == "error":
                tail = buf.flush()
                if tail:
                    yield normalize_delimiters(tail)
                yield f"\n\n**error:** {e['message']}"
    finally:
        try:
            kern.km.shutdown_kernel(now=True)
        except Exception:
            pass


def _chunk(content, *, model, role=False, finish=None):
    delta = {}
    if role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content
    body = {
        "id": "chatcmpl-pudding", "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(body: dict):
    messages = body.get("messages", [])
    model = body.get("model", MODEL_ID)
    stream = body.get("stream", False)
    # The last user turn is the problem (each message = a fresh problem; solver is stateless).
    problem = next((m.get("content", "") for m in reversed(messages)
                    if m.get("role") == "user"), "")

    if stream:
        def sse():
            yield _chunk(None, model=model, role=True)
            for piece in _render_pieces(problem):
                yield _chunk(piece, model=model)
            yield _chunk(None, model=model, finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(sse(), media_type="text/event-stream")

    content = "".join(_render_pieces(problem))
    return JSONResponse({
        "id": "chatcmpl-pudding", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
