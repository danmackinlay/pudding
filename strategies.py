"""Generalist strategies (the friction-free rungs of the trust ladder) — ASYNC-native.

Async generators that emit the shared streaming event envelope (streaming.py). Async because
the work is IO-bound (provider HTTP): the audition runs k samples / many problems concurrently
on one event loop via AsyncOpenAI, and `asyncio.wait_for` gives cancellable timeouts — no
thread-per-call, no orphaned-thread hangs. (The FastAPI shim in Phase B can consume these async
generators directly for streaming.)

  cot          rung 1  — one chat call; reason in-context; read the last \\boxed{}.
  self_verify  rung 2  — cot, then a critique-and-restate pass over the candidate.
  tools        rung 3  — generalist + OUR kernel via native tool-calls. Gated (PLAN.md §3/§5).

Every call is streamed so we can measure **TTFT** and **decode tok/s** (the instrumentation the
audition needs to localise slowness). The answer is read from visible `content`, falling back to
`reasoning_content`/`reasoning` only when content has no \\boxed{} (thinking ate the budget).
"""
import time
from typing import AsyncIterator

from providers import make_async_client
from solver_loop import extract_boxed   # reuse the brace-balanced \boxed{} parser
from streaming import ev

COT_SYS = ("Solve the problem. Reason step by step, then give the final answer as "
           "\\boxed{...}. Put ONLY the final answer inside \\boxed{}.")

VERIFY_SYS = ("You are a meticulous mathematician checking a candidate solution. Find any "
              "error in its reasoning or arithmetic, then give the correct final answer as "
              "\\boxed{...}. If the candidate is already correct, restate its boxed answer.")


def _extra(obj, key: str):
    """Read a vendor extension field off an OpenAI SDK model (typed attr or model_extra)."""
    v = getattr(obj, key, None)
    if v is None:
        extra = getattr(obj, "model_extra", None)
        if extra:
            v = extra.get(key)
    return v


def _reasoning(obj) -> str:
    """Reasoning trace under whichever name the provider uses (DeepSeek/Qwen/Kimi:
    `reasoning_content`; OpenRouter normalizes to `reasoning`)."""
    for key in ("reasoning_content", "reasoning"):
        v = _extra(obj, key)
        if v:
            return v
    return ""


async def _chat(client, model, messages, temperature, max_tokens, seed):
    """One streamed chat call. Yields ('delta', visible_text), then ('done', info) with
    {text, reasoning, finish_reason, completion_tokens, prompt_tokens, ttft_s, gen_s,
    decode_tok_s}. Always streams so TTFT/decode-rate are measurable."""
    args = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens,
                stream=True, stream_options={"include_usage": True})
    if seed is not None:
        args["seed"] = seed

    t0 = time.perf_counter()
    t_first = None
    text = reasoning = ""
    finish = None
    ctoks = ptoks = None
    stream = await client.chat.completions.create(**args)
    async for chunk in stream:
        if chunk.choices:
            d = chunk.choices[0].delta
            piece = getattr(d, "content", None) or ""
            rpiece = _reasoning(d)
            if (piece or rpiece) and t_first is None:
                t_first = time.perf_counter()         # first token of either channel
            if piece:
                text += piece
                yield "delta", piece
            if rpiece:
                reasoning += rpiece
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
        if getattr(chunk, "usage", None):             # final usage chunk (include_usage)
            ctoks, ptoks = chunk.usage.completion_tokens, chunk.usage.prompt_tokens
    t_done = time.perf_counter()
    ttft = (t_first - t0) if t_first else None
    gen = (t_done - t_first) if t_first else (t_done - t0)
    decode = (ctoks / gen) if (ctoks and gen and gen > 0) else None
    yield "done", {"text": text, "reasoning": reasoning, "finish_reason": finish,
                   "completion_tokens": ctoks, "prompt_tokens": ptoks,
                   "ttft_s": round(ttft, 2) if ttft is not None else None,
                   "gen_s": round(gen, 2),
                   "decode_tok_s": round(decode, 1) if decode is not None else None}


async def _drain(client, model, messages, temperature, max_tokens, seed):
    """Run one _chat, forwarding its visible text as reasoning_delta events; return the info."""
    info = {}
    async for kind, payload in _chat(client, model, messages, temperature, max_tokens, seed):
        if kind == "delta":
            yield "reasoning_delta", payload
        else:
            info = payload
    yield "info", info


async def cot_stream(problem, *, client, model, temperature, seed, max_tokens) -> AsyncIterator[dict]:
    """Rung 1: one chat call → last \\boxed{}."""
    t0 = time.perf_counter()
    msgs = [{"role": "system", "content": COT_SYS}, {"role": "user", "content": problem}]
    info = {}
    async for kind, payload in _drain(client, model, msgs, temperature, max_tokens, seed):
        if kind == "reasoning_delta":
            yield ev("reasoning_delta", text=payload)
        else:
            info = payload
    boxed = extract_boxed(info.get("text", "")) or extract_boxed(info.get("reasoning", ""))
    yield ev("final_answer", boxed=boxed, transcript=info.get("text") or info.get("reasoning", ""),
             elapsed_s=round(time.perf_counter() - t0, 2),
             completion_tokens=info.get("completion_tokens") or 0,
             truncated=info.get("finish_reason") == "length",
             ttft_s=info.get("ttft_s"), decode_tok_s=info.get("decode_tok_s"),
             calls=[{"ttft_s": info.get("ttft_s"), "gen_s": info.get("gen_s"),
                     "tok": info.get("completion_tokens")}])


async def self_verify_stream(problem, *, client, model, temperature, seed, max_tokens) -> AsyncIterator[dict]:
    """Rung 2: cot, then a critique-and-restate pass over the candidate."""
    t0 = time.perf_counter()
    msgs = [{"role": "system", "content": COT_SYS}, {"role": "user", "content": problem}]
    c = {}
    async for kind, payload in _drain(client, model, msgs, temperature, max_tokens, seed):
        if kind == "reasoning_delta":
            yield ev("reasoning_delta", text=payload)
        else:
            c = payload
    candidate = c.get("text") or c.get("reasoning") or ""

    yield ev("reasoning_delta", text="\n\n---\n**Verification pass**\n\n")
    vmsgs = [{"role": "system", "content": VERIFY_SYS},
             {"role": "user", "content": f"Problem:\n{problem}\n\nCandidate solution:\n{candidate}"}]
    vseed = seed + 1 if seed is not None else None
    v = {}
    async for kind, payload in _drain(client, model, vmsgs, temperature, max_tokens, vseed):
        if kind == "reasoning_delta":
            yield ev("reasoning_delta", text=payload)
        else:
            v = payload

    boxed = extract_boxed(v.get("text", "")) or extract_boxed(v.get("reasoning", "")) \
        or extract_boxed(candidate)
    ctoks = (c.get("completion_tokens") or 0) + (v.get("completion_tokens") or 0)
    calls = [{"ttft_s": c.get("ttft_s"), "gen_s": c.get("gen_s"), "tok": c.get("completion_tokens")},
             {"ttft_s": v.get("ttft_s"), "gen_s": v.get("gen_s"), "tok": v.get("completion_tokens")}]
    yield ev("final_answer", boxed=boxed,
             transcript=f"{candidate}\n\n--- verification ---\n{v.get('text') or v.get('reasoning', '')}",
             elapsed_s=round(time.perf_counter() - t0, 2), completion_tokens=ctoks,
             truncated="length" in (c.get("finish_reason"), v.get("finish_reason")),
             ttft_s=c.get("ttft_s"), decode_tok_s=v.get("decode_tok_s"), calls=calls)


async def generalist_stream(problem, *, strategy, provider=None, client=None, model=None,
                            temperature=0.0, seed=None, max_tokens=8192) -> AsyncIterator[dict]:
    """Dispatch a non-fence (generalist) strategy. Errors surface as an `error` event so the
    consumer never hangs."""
    try:
        if model is None:
            raise ValueError(f"strategy {strategy!r} needs an explicit model (pass --model)")
        client = client or make_async_client(provider)
        if strategy == "cot":
            async for e in cot_stream(problem, client=client, model=model,
                                      temperature=temperature, seed=seed, max_tokens=max_tokens):
                yield e
        elif strategy == "self_verify":
            async for e in self_verify_stream(problem, client=client, model=model,
                                              temperature=temperature, seed=seed,
                                              max_tokens=max_tokens):
                yield e
        elif strategy == "tools":
            raise NotImplementedError(
                "tools rung not built — gated on the audition (PLAN.md §3/§5). When CoT "
                "numbers justify it, wrap smolagents rather than hand-rolling the loop.")
        else:
            raise ValueError(f"unknown strategy {strategy!r}")
    except Exception as e:  # noqa: BLE001 — surface as an event, don't crash the consumer
        yield ev("error", message=f"{type(e).__name__}: {e}")
