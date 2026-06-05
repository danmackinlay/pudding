"""Unit tests for the async generalist strategies (cot / self_verify) and the dispatch.

No network: a streaming AsyncFakeClient returns scripted chat chunks, so we exercise the
envelope, the reasoning_content/reasoning fallback, token accounting, and the
solve_one_async dispatch offline. Async generators are driven via asyncio.run.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solver_loop import extract_boxed, solve_one_async          # noqa: E402
from strategies import cot_stream, self_verify_stream, generalist_stream  # noqa: E402


# --- a scripted, network-free *streaming* async chat client ----------------
# Scripted items are (content, reasoning) or (content, reasoning, reasoning_key); the third
# element puts the trace under OpenRouter's `reasoning` instead of `reasoning_content`.
class _Delta:
    def __init__(self, content=None, reasoning=None, reasoning_key="reasoning_content"):
        self.content = content
        if reasoning is not None:
            setattr(self, reasoning_key, reasoning)


class _ChunkChoice:
    def __init__(self, delta, finish=None):
        self.delta = delta
        self.finish_reason = finish


class _Usage:
    def __init__(self):
        self.completion_tokens = 42
        self.prompt_tokens = 7


class _Chunk:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage


class _AsyncStream:
    """Async-iterable of chunks: (optional reasoning) → (optional content) → finish → usage."""
    def __init__(self, item):
        content = item[0]
        reasoning = item[1] if len(item) > 1 else None
        key = item[2] if len(item) > 2 else "reasoning_content"
        chunks = []
        if reasoning:
            chunks.append(_Chunk([_ChunkChoice(_Delta(reasoning=reasoning, reasoning_key=key))]))
        if content:
            chunks.append(_Chunk([_ChunkChoice(_Delta(content=content))]))
        chunks.append(_Chunk([_ChunkChoice(_Delta(), finish="stop")]))
        chunks.append(_Chunk([], usage=_Usage()))
        self._chunks, self._i = chunks, 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._i]
        self._i += 1
        return c


class _AsyncCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    async def create(self, **kw):
        return _AsyncStream(self._scripted.pop(0))


class AsyncFakeClient:
    def __init__(self, scripted):
        self.chat = type("C", (), {"completions": _AsyncCompletions(scripted)})()


def _run(coro):
    return asyncio.run(coro)


async def _collect(agen):
    return [e async for e in agen]


def _final(events):
    return next(e for e in events if e["type"] == "final_answer")


# --- cot -------------------------------------------------------------------
def test_cot_reads_boxed_and_usage():
    client = AsyncFakeClient([("Reason… therefore \\boxed{42}.", None)])
    evts = _run(_collect(cot_stream("q", client=client, model="m", temperature=0.0,
                                    seed=None, max_tokens=100)))
    f = _final(evts)
    assert f["boxed"] == "42"
    assert f["completion_tokens"] == 42
    assert f["truncated"] is False


def test_cot_falls_back_to_reasoning_content_when_content_empty():
    client = AsyncFakeClient([("", "the work… \\boxed{7}")])
    evts = _run(_collect(cot_stream("q", client=client, model="m", temperature=0.0,
                                    seed=None, max_tokens=100)))
    assert _final(evts)["boxed"] == "7"


def test_cot_handles_openrouter_reasoning_key():
    # OpenRouter exposes the trace as `reasoning`; empty content forces the \boxed{} fallback.
    client = AsyncFakeClient([("", "work… \\boxed{5}", "reasoning")])
    evts = _run(_collect(cot_stream("q", client=client, model="m", temperature=0.0,
                                    seed=None, max_tokens=100)))
    assert _final(evts)["boxed"] == "5"


# --- self_verify -----------------------------------------------------------
def test_self_verify_uses_the_corrected_answer():
    client = AsyncFakeClient([("Try \\boxed{41}.", None),
                              ("That's off by two; correct is \\boxed{43}.", None)])
    evts = _run(_collect(self_verify_stream("q", client=client, model="m", temperature=0.0,
                                            seed=1, max_tokens=100)))
    f = _final(evts)
    assert f["boxed"] == "43"
    assert f["candidate_boxed"] == "41"      # pass-1 answer, for the shim's ✓/⚠ verdict
    assert f["completion_tokens"] == 84      # both passes summed


def test_thinking_trace_surfaces_as_thinking_delta():
    # reasoning_content (the hidden trace) must stream on its own channel, separate from content.
    client = AsyncFakeClient([("answer \\boxed{8}", "I should add the two parts…")])
    evts = _run(_collect(cot_stream("q", client=client, model="m", temperature=0.0,
                                    seed=None, max_tokens=100)))
    thinking = "".join(e["text"] for e in evts if e["type"] == "thinking_delta")
    content = "".join(e["text"] for e in evts if e["type"] == "reasoning_delta")
    assert "I should add" in thinking and "I should add" not in content
    assert "\\boxed{8}" in content


# --- dispatch (solve_one_async) --------------------------------------------
def test_solve_one_async_routes_cot_and_reports_tokens():
    client = AsyncFakeClient([("the answer is \\boxed{99}", None)])
    r = _run(solve_one_async("q", strategy="cot", model="m", client=client))
    assert r["boxed"] == "99"
    assert r["completion_tokens"] == 42
    assert r["error"] is None


def test_generalist_requires_a_model():
    evts = _run(_collect(generalist_stream("q", strategy="cot", model=None)))
    assert evts[-1]["type"] == "error" and "model" in evts[-1]["message"]


def test_tools_rung_is_gated():
    evts = _run(_collect(generalist_stream("q", strategy="tools", model="m",
                                           client=AsyncFakeClient([]))))
    assert evts[-1]["type"] == "error" and "tools" in evts[-1]["message"].lower()
