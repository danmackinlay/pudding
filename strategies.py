"""Generalist strategies — the lower, friction-free rungs of the trust ladder (PLAN.md §1).

Chat-endpoint producers that emit the SAME streaming event envelope as the TIR loop
(streaming.py), so eval.py, fanout.py, and the chat shim drive them with no changes — a new
strategy that emits the envelope inherits grading, fan-out, and the UI for free.

  cot          rung 1  — one chat call; reason in-context; read the last \\boxed{}. No
                          executor. A strong reasoner is competitive on competition maths
                          this way; maj@k over seeds (fanout.py) is the confidence signal.
  self_verify  rung 2  — cot, then a second pass that critiques the candidate and restates
                          (or corrects) the boxed answer. Soft verification, no compiler.
  tools        rung 3  — generalist + OUR Python kernel via native tool-calls. NOT built:
                          gated on the audition (only if cot fumbles exact computation). When
                          built, borrow smolagents' loop rather than hand-maintaining the
                          per-provider tool-call/reasoning quirks — PLAN.md §3 / §6.2.

Reasoning channel: generalists keep their thinking in `reasoning_content` (Kimi/DeepSeek/
Qwen3 'thinking'), separate from `content`. We read the ANSWER from `content` and fall back to
`reasoning_content` only when content has no \\boxed{} (the "thinking ate the token budget"
case). Single-shot strategies never echo reasoning_content back, side-stepping the per-model
round-trip rules (DeepSeek 400s on it; Kimi needs it resent) — PLAN.md §6.2.
"""
import time
from typing import Iterator

from providers import make_client
from solver_loop import extract_boxed   # reuse the brace-balanced \boxed{} parser
from streaming import ev

COT_SYS = ("Solve the problem. Reason step by step, then give the final answer as "
           "\\boxed{...}. Put ONLY the final answer inside \\boxed{}.")

VERIFY_SYS = ("You are a meticulous mathematician checking a candidate solution. Find any "
              "error in its reasoning or arithmetic, then give the correct final answer as "
              "\\boxed{...}. If the candidate is already correct, restate its boxed answer.")


def _extra(obj, key: str):
    """Read a vendor extension field (e.g. reasoning_content) off an OpenAI SDK model,
    whether the SDK typed it or stashed it in model_extra."""
    v = getattr(obj, key, None)
    if v is None:
        extra = getattr(obj, "model_extra", None)
        if extra:
            v = extra.get(key)
    return v


def _reasoning(obj) -> str:
    """The reasoning trace, under whichever name the provider uses: DeepSeek/Qwen/Kimi emit
    `reasoning_content`; OpenRouter normalizes it to `reasoning`. (Only used as a \\boxed{}
    fallback when visible content is empty.)"""
    for key in ("reasoning_content", "reasoning"):
        v = _extra(obj, key)
        if v:
            return v
    return ""


def _chat(client, model, messages, temperature, max_tokens, seed, stream):
    """One chat call. Yields ('delta', visible_text) per chunk, then ('done', info) with
    {text, reasoning, finish_reason, completion_tokens, prompt_tokens}. Only visible `content`
    is streamed as deltas; `reasoning_content` is captured but not surfaced as the answer."""
    args = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if seed is not None:
        args["seed"] = seed

    if not stream:
        r = client.chat.completions.create(**args)
        msg = r.choices[0].message
        text = msg.content or ""
        u = getattr(r, "usage", None)
        if text:
            yield "delta", text
        yield "done", {"text": text, "reasoning": _reasoning(msg),
                       "finish_reason": r.choices[0].finish_reason,
                       "completion_tokens": getattr(u, "completion_tokens", None),
                       "prompt_tokens": getattr(u, "prompt_tokens", None)}
        return

    text, reasoning, finish, ctoks, ptoks = "", "", None, None, None
    s = client.chat.completions.create(stream=True, stream_options={"include_usage": True}, **args)
    for chunk in s:
        if chunk.choices:
            d = chunk.choices[0].delta
            piece = (getattr(d, "content", None) or "")
            if piece:
                text += piece
                yield "delta", piece
            rpiece = _reasoning(d)
            if rpiece:
                reasoning += rpiece
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
        if getattr(chunk, "usage", None):          # final usage chunk (include_usage)
            ctoks, ptoks = chunk.usage.completion_tokens, chunk.usage.prompt_tokens
    yield "done", {"text": text, "reasoning": reasoning, "finish_reason": finish,
                   "completion_tokens": ctoks, "prompt_tokens": ptoks}


def _run_pass(client, model, messages, temperature, max_tokens, seed, stream):
    """Drive one _chat, streaming its visible text as reasoning_delta events. Returns the
    'done' info dict (so the caller can stitch passes and read usage/finish_reason)."""
    info = {}
    for kind, payload in _chat(client, model, messages, temperature, max_tokens, seed, stream):
        if kind == "delta":
            yield ev("reasoning_delta", text=payload)
        else:
            info = payload
    return info


def cot_stream(problem, *, client, model, temperature, seed, max_tokens, stream):
    """Rung 1: one chat call → last \\boxed{}."""
    t0 = time.time()
    msgs = [{"role": "system", "content": COT_SYS}, {"role": "user", "content": problem}]
    info = yield from _run_pass(client, model, msgs, temperature, max_tokens, seed, stream)
    text, reasoning = info.get("text", ""), info.get("reasoning", "")
    boxed = extract_boxed(text) or extract_boxed(reasoning)   # fallback: thinking ate the budget
    yield ev("final_answer", boxed=boxed, transcript=text or reasoning,
             elapsed_s=round(time.time() - t0, 1),
             completion_tokens=info.get("completion_tokens") or 0,
             truncated=info.get("finish_reason") == "length")


def self_verify_stream(problem, *, client, model, temperature, seed, max_tokens, stream):
    """Rung 2: cot, then a critique-and-restate pass over the candidate."""
    t0 = time.time()
    msgs = [{"role": "system", "content": COT_SYS}, {"role": "user", "content": problem}]
    c = yield from _run_pass(client, model, msgs, temperature, max_tokens, seed, stream)
    candidate = c.get("text") or c.get("reasoning") or ""

    yield ev("reasoning_delta", text="\n\n---\n**Verification pass**\n\n")
    vmsgs = [{"role": "system", "content": VERIFY_SYS},
             {"role": "user",
              "content": f"Problem:\n{problem}\n\nCandidate solution:\n{candidate}"}]
    vseed = seed + 1 if seed is not None else None
    v = yield from _run_pass(client, model, vmsgs, temperature, max_tokens, vseed, stream)
    vtext, vreason = v.get("text", ""), v.get("reasoning", "")

    boxed = extract_boxed(vtext) or extract_boxed(vreason) or extract_boxed(candidate)
    ctoks = (c.get("completion_tokens") or 0) + (v.get("completion_tokens") or 0)
    truncated = "length" in (c.get("finish_reason"), v.get("finish_reason"))
    yield ev("final_answer", boxed=boxed,
             transcript=f"{candidate}\n\n--- verification ---\n{vtext or vreason}",
             elapsed_s=round(time.time() - t0, 1), completion_tokens=ctoks, truncated=truncated)


def generalist_stream(problem, *, strategy, provider=None, client=None, model=None,
                      temperature=0.0, seed=None, max_tokens=8192, stream=True) -> Iterator[dict]:
    """Dispatch a non-fence (generalist) strategy. Errors surface as an `error` event so
    eval/UI never hang (mirrors solver_loop.solve_stream)."""
    try:
        if model is None:
            raise ValueError(f"strategy {strategy!r} needs an explicit model (the audition "
                             "names the generalist — pass --model)")
        client = client or make_client(provider)
        if strategy == "cot":
            yield from cot_stream(problem, client=client, model=model, temperature=temperature,
                                  seed=seed, max_tokens=max_tokens, stream=stream)
        elif strategy == "self_verify":
            yield from self_verify_stream(problem, client=client, model=model,
                                          temperature=temperature, seed=seed,
                                          max_tokens=max_tokens, stream=stream)
        elif strategy == "tools":
            raise NotImplementedError(
                "tools rung not built — gated on the audition (PLAN.md §3/§5). When CoT "
                "numbers justify it, wrap smolagents rather than hand-rolling the loop.")
        else:
            raise ValueError(f"unknown strategy {strategy!r}")
    except Exception as e:  # noqa: BLE001 — surface as an event, don't crash the consumer
        yield ev("error", message=f"{type(e).__name__}: {e}")
