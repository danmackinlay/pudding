"""Orchestrator (PRIMARY): the TIR solver loop — remote tokens, local executor.

This is the spine of the repo. The model writes Python in ```python fences; we run each
block in a stateful IPython kernel and splice the result back inside a ```output fence;
repeat until the model emits no fresh code. The prover (prover_loop.py) is the SAME shape
with a Lean verifier in place of the Python kernel and a different halting rule.

`executor` is anything with `.run(code) -> str`: the local `Kernel` below by default, or
the remote `executor/modal_executor.py` Sandbox when the computation is heavy.

UNTESTED. See PLAN.md §5 for the testing order (the solver is steps 1–5).
"""
import asyncio
import os
import time
from typing import Iterator

from openai import OpenAI

from providers import make_client  # provider registry (model-server axis); see providers.py
from streaming import ev  # shared event envelope (also used by the UI shim + stage-2 prover)

MODEL = "Qwen/Qwen2.5-Math-7B-Instruct"

# Generous generation budget by default. A TIR answer carries reasoning + code + the final
# \boxed{}; a stingy cap truncates multi-step solutions before the answer ever appears
# (1024/round dropped ~20% of GSM8K to empty answers). These are per-round / per-loop maxima.
MAX_TOKENS = 8192
MAX_CALLS = 8

SYS = ("Please integrate natural language reasoning with programs to solve the "
       "problem above, and put your final answer within \\boxed{}.")

# Qwen2.5-Math writes a ```python block and expects the result in a ```output block.
# (OpenMath-Nemotron uses <tool_call>…</tool_call> instead — read the tags off the
# model's docs, not its name; parametrize here if you swap models. PLAN.md §2.1.)
TICK = chr(96) * 3
CODE_OPEN, OUT_OPEN, CLOSE = TICK + "python", TICK + "output", TICK


class Kernel:
    """Local stateful IPython kernel: variables persist across code blocks, so a value
    defined in the first block is visible in the third. A fresh subprocess per block
    would break multi-step solutions."""

    def __init__(self):
        from jupyter_client import KernelManager
        self.km = KernelManager()
        self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()

    def run(self, code, timeout=10):
        self.kc.execute(code)
        chunks = []
        while True:
            try:
                m = self.kc.get_iopub_msg(timeout=timeout)
            except Exception:
                chunks.append("[timeout]")
                break
            t, c = m["msg_type"], m["content"]
            if t == "stream":
                chunks.append(c["text"])
            elif t in ("execute_result", "display_data"):
                chunks.append(c["data"].get("text/plain", ""))
            elif t == "error":
                chunks.append("\n".join(c["traceback"]))
            elif t == "status" and c["execution_state"] == "idle":
                break
        return "".join(chunks)[:1000]  # cap so a runaway print can't flood context


def last_code(text: str):
    return text.rsplit(CODE_OPEN, 1)[1].split(CLOSE, 1)[0] if CODE_OPEN in text else None


def extract_boxed(text: str) -> str | None:
    """Content of the LAST \\boxed{...}, brace-balanced so nested braces survive.

    The solver's answer; maj@k (fanout.py) votes over these across k chains.
    """
    start = text.rfind("\\boxed{")
    if start == -1:
        return None
    i, depth, out = start + len("\\boxed{"), 1, []
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
        i += 1
    return "".join(out).strip() if depth == 0 else None


# Transient provider hiccups (Featherless 503 / "temporarily at capacity" / 429 rate limit)
# — retry with exponential backoff rather than surfacing them. A first cut of the Stage 1.6
# retry policy, pulled forward because the flakiness is frequent enough to disrupt the UI.
_TRANSIENT = ("capacity", "temporarily", "overloaded", "rate limit", "try again", "timeout")


def _is_transient(e: Exception) -> bool:
    msg = str(e).lower()
    return any(k in msg for k in _TRANSIENT) or "503" in msg or "429" in msg or "502" in msg


def _create(client, args, stream, retries: int = 4, base: float = 2.0):
    """`completions.create` with backoff on transient provider errors (the request is
    rejected pre-generation, so retrying at the call site is safe — no partial output)."""
    for attempt in range(retries + 1):
        try:
            if stream:
                return client.completions.create(
                    stream=True, stream_options={"include_usage": True}, **args)
            return client.completions.create(**args)
        except Exception as e:  # noqa: BLE001
            if not _is_transient(e) or attempt == retries:
                raise
            time.sleep(base * (2 ** attempt))  # 2s, 4s, 8s, 16s


def _generate(client, model, prompt, temperature, max_tokens, seed, stream):
    """One model call. Yields ("delta", text) chunks, then a final ("done", info) with
    {text, finish_reason, completion_tokens, prompt_tokens}. `stream=False` yields the whole
    text as a single delta (so the blocking `solve()` path is byte-identical to before)."""
    args = dict(model=model, prompt=prompt, temperature=temperature, max_tokens=max_tokens,
                seed=seed, stop=[OUT_OPEN, "\n\n---"])
    if not stream:
        r = _create(client, args, stream=False)
        u = getattr(r, "usage", None)
        yield "delta", r.choices[0].text
        yield "done", {"text": r.choices[0].text, "finish_reason": r.choices[0].finish_reason,
                       "completion_tokens": getattr(u, "completion_tokens", None),
                       "prompt_tokens": getattr(u, "prompt_tokens", None)}
        return
    text, finish, ctoks, ptoks = "", None, None, None
    s = _create(client, args, stream=True)
    for chunk in s:
        if chunk.choices:
            delta = chunk.choices[0].text or ""
            if delta:
                text += delta
                yield "delta", delta
            if chunk.choices[0].finish_reason:
                finish = chunk.choices[0].finish_reason
        if getattr(chunk, "usage", None):  # final usage chunk (include_usage)
            ctoks, ptoks = chunk.usage.completion_tokens, chunk.usage.prompt_tokens
    yield "done", {"text": text, "finish_reason": finish,
                   "completion_tokens": ctoks, "prompt_tokens": ptoks}


def solve_stream(problem: str, executor=None, max_calls: int = MAX_CALLS,
                 client: OpenAI | None = None, temperature: float = 0.0,
                 seed: int | None = None, model: str | None = None,
                 max_tokens: int = MAX_TOKENS, stream: bool = True,
                 strategy: str = "tir_fence", provider: str | None = None) -> Iterator[dict]:
    """The TIR loop as a stream of events (the shared envelope; see streaming.py).

    Yields: `reasoning_delta{text}` per token chunk, `code{lang,code}` before each
    execution, `tool_result{output}` after, a final `final_answer{boxed, transcript,
    elapsed_s, completion_tokens, truncated}`, or `error{message}` on failure. This is what
    the UI shim consumes; `solve()` below consumes it in non-stream mode for eval/fanout.
    """
    # Generalist rungs (cot/self_verify) are async-native now (strategies.py + solve_one_async);
    # this sync streaming loop is the TIR/UI path only.
    if strategy != "tir_fence":
        yield ev("error", message=f"strategy {strategy!r} is async-only — use solve_one_async "
                                   "(the audition: eval.py / audition.py)")
        return

    client = client or make_client(provider)
    model = model or MODEL
    executor = executor or Kernel()
    run = getattr(executor.run, "remote", executor.run)  # Modal class vs local object

    prompt = (f"<|im_start|>system\n{SYS}<|im_end|>\n"
              f"<|im_start|>user\n{problem}<|im_end|>\n<|im_start|>assistant\n")
    t0, comp_tokens, truncated, prev_code = time.time(), 0, False, None
    try:
        for _ in range(max_calls + 1):
            round_text, info = "", {}
            for kind, payload in _generate(client, model, prompt, temperature,
                                           max_tokens, seed, stream):
                if kind == "delta":
                    round_text += payload
                    yield ev("reasoning_delta", text=payload)
                else:
                    info = payload
            prompt += round_text
            comp_tokens += info.get("completion_tokens") or 0
            truncated = truncated or info.get("finish_reason") == "length"
            code = last_code(round_text)
            if code is None or not code.strip():  # no fresh code (or empty ``` fence) -> done
                break
            if code.strip() == prev_code:  # same block again -> no progress; stop, don't burn the budget
                break
            prev_code = code.strip()
            yield ev("code", lang="python", code=code.strip())
            out = run(code)                        # run the original (unstripped) block
            prompt += f"{OUT_OPEN}\n{out}\n{CLOSE}\n"
            yield ev("tool_result", output=out)
        yield ev("final_answer", boxed=extract_boxed(prompt), transcript=prompt,
                 elapsed_s=round(time.time() - t0, 1), completion_tokens=comp_tokens,
                 truncated=truncated)
    except Exception as e:  # noqa: BLE001 — surface as an event so the UI never hangs
        yield ev("error", message=f"{type(e).__name__}: {e}")


def solve(problem: str, executor=None, max_calls: int = MAX_CALLS, client: OpenAI | None = None,
          temperature: float = 0.0, seed: int | None = None, model: str | None = None,
          max_tokens: int = MAX_TOKENS, strategy: str = "tir_fence",
          provider: str | None = None) -> str:
    """Blocking TIR solve → full transcript (answer = last \\boxed{...}, via extract_boxed).

    A thin wrapper over `solve_stream` in **non-stream** mode, so `eval.py`/`fanout.py` get
    byte-identical behavior to before (the streaming path is only for the UI shim).
    `executor` has `.run(code) -> str` (default: a local `Kernel`); for the remote executor
    pass `modal.Cls.from_name("pudding-executor", "Executor")()` (the `.remote` adapter is
    handled). `temperature`>0 + a distinct `seed` per call is what diverges maj@k chains.
    """
    transcript = ""
    for evt in solve_stream(problem, executor=executor, max_calls=max_calls, client=client,
                            temperature=temperature, seed=seed, model=model,
                            max_tokens=max_tokens, stream=False, strategy=strategy,
                            provider=provider):
        if evt["type"] == "final_answer":
            transcript = evt["transcript"]
        elif evt["type"] == "error":
            raise RuntimeError(evt["message"])  # preserve "raises on failure" for eval/fanout
    return transcript


def solve_one(problem: str, *, executor=None, model: str | None = None,
              provider: str | None = None, strategy: str = "tir_fence",
              temperature: float = 0.0, seed: int | None = None, max_tokens: int = MAX_TOKENS,
              max_calls: int = MAX_CALLS, client: OpenAI | None = None) -> dict:
    """One chain → a detailed result dict, the cost-aware sibling of `solve()`:
    {boxed, transcript, completion_tokens, truncated, elapsed_s, error}. Surfaces token usage
    (the per-token $ signal) and never raises — failures land in result['error']. The audition
    (eval.py / audition.py) uses this; `solve()` stays for fanout's transcript contract."""
    result = {"boxed": None, "transcript": "", "completion_tokens": 0,
              "truncated": False, "elapsed_s": 0.0, "error": None}
    for evt in solve_stream(problem, executor=executor, max_calls=max_calls, client=client,
                            temperature=temperature, seed=seed, model=model,
                            max_tokens=max_tokens, stream=False, strategy=strategy,
                            provider=provider):
        if evt["type"] == "final_answer":
            result.update(boxed=evt.get("boxed"), transcript=evt.get("transcript", ""),
                          completion_tokens=evt.get("completion_tokens") or 0,
                          truncated=bool(evt.get("truncated")),
                          elapsed_s=evt.get("elapsed_s") or 0.0)
        elif evt["type"] == "error":
            result["error"] = evt["message"]
    return result


async def solve_one_async(problem: str, *, strategy: str = "tir_fence", model: str | None = None,
                          provider: str | None = None, temperature: float = 0.0,
                          seed: int | None = None, max_tokens: int = MAX_TOKENS,
                          max_calls: int = MAX_CALLS, client=None) -> dict:
    """Async single chain → {boxed, transcript, completion_tokens, truncated, elapsed_s,
    ttft_s, decode_tok_s, error}. Generalist rungs run native-async (AsyncOpenAI); tir_fence
    runs its blocking-kernel loop in a worker thread. The async audition (eval.py) drives this."""
    base = {"boxed": None, "transcript": "", "completion_tokens": 0, "truncated": False,
            "elapsed_s": 0.0, "ttft_s": None, "decode_tok_s": None, "error": None, "thinking": ""}
    if strategy in ("cot", "self_verify", "tools"):
        from strategies import generalist_stream
        async for evt in generalist_stream(problem, strategy=strategy, provider=provider,
                                            client=client, model=model, temperature=temperature,
                                            seed=seed, max_tokens=max_tokens):
            if evt["type"] == "final_answer":
                base.update(boxed=evt.get("boxed"), transcript=evt.get("transcript", ""),
                            thinking=evt.get("reasoning") or "",       # the model's hidden CoT
                            completion_tokens=evt.get("completion_tokens") or 0,
                            truncated=bool(evt.get("truncated")),
                            elapsed_s=evt.get("elapsed_s") or 0.0,
                            ttft_s=evt.get("ttft_s"), decode_tok_s=evt.get("decode_tok_s"))
            elif evt["type"] == "error":
                base["error"] = evt["message"]
        return base
    # tir_fence: a blocking IPython kernel → run the sync chain in a worker thread
    return await asyncio.to_thread(_tir_solve_one_sync, problem, model, provider,
                                   temperature, seed, max_tokens, max_calls)


def _tir_solve_one_sync(problem, model, provider, temperature, seed, max_tokens, max_calls) -> dict:
    """The TIR chain on a fresh local Kernel (sync); returns the solve_one dict + a coarse rate."""
    kern = Kernel()
    try:
        r = solve_one(problem, executor=kern, model=model, provider=provider,
                      strategy="tir_fence", temperature=temperature, seed=seed,
                      max_tokens=max_tokens, max_calls=max_calls)
    finally:
        try:
            kern.km.shutdown_kernel(now=True)
        except Exception:
            pass
    el = r.get("elapsed_s") or 0.0
    r.setdefault("ttft_s", None)
    r["decode_tok_s"] = round((r.get("completion_tokens") or 0) / el, 1) if el > 0 else None
    return r


if __name__ == "__main__":
    # Smoke test against a local kernel (needs jupyter_client + ipykernel installed).
    import sys
    problem = " ".join(sys.argv[1:]) or "Find the remainder when 7^999 is divided by 1000."
    transcript = solve(problem)
    print(transcript)
    print("=" * 60)
    print(f"boxed answer: {extract_boxed(transcript)}")
