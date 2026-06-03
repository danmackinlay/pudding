"""Offline test for solver_loop.solve_stream / solve — fake client + fake executor.

Locks the invariants that protect the verified eval/fanout path:
  - solve_stream emits the documented event sequence,
  - stream=True and stream=False produce the SAME transcript,
  - solve() (the wrapper) returns that transcript and the boxed answer.
No network, no kernel. Run: direnv exec . uv run python -m pytest tests/test_events.py
                       or: direnv exec . uv run python tests/test_events.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import solver_loop
from solver_loop import solve, solve_stream, extract_boxed

# Two canned model turns: round 1 emits a ```python block (generation stops before the
# ```output fence, as the real stop sequence does); round 2 concludes with \boxed{}.
ROUND1 = "I'll compute it.\n```python\nprint(pow(7,999,1000))\n```\n"
ROUND2 = "The remainder is \\boxed{143}."


class _Usage:
    def __init__(self, c=7, p=10):
        self.completion_tokens, self.prompt_tokens = c, p


class _Choice:
    def __init__(self, text, finish="stop"):
        self.text, self.finish_reason = text, finish


class _Completion:
    def __init__(self, text):
        self.choices, self.usage = [_Choice(text)], _Usage()


class _Chunk:
    def __init__(self, text=None, finish=None, usage=None):
        self.choices = [] if usage is not None else [_Choice(text or "", finish)]
        self.usage = usage


class _FakeCompletions:
    def __init__(self, scripts):
        self.scripts, self.i = scripts, 0

    def create(self, stream=False, stream_options=None, **kw):
        text = self.scripts[self.i]
        self.i += 1
        if not stream:
            return _Completion(text)

        def gen():
            n = max(1, len(text) // 3)              # arbitrary chunking
            parts = [text[j:j + n] for j in range(0, len(text), n)]
            for k, p in enumerate(parts):
                yield _Chunk(text=p, finish="stop" if k == len(parts) - 1 else None)
            yield _Chunk(usage=_Usage())            # final include_usage chunk
        return gen()


class _FakeClient:
    def __init__(self, scripts):
        self.completions = _FakeCompletions(scripts)


class _FakeExecutor:
    def run(self, code):
        assert "pow(7,999,1000)" in code            # the stripped block was passed through
        return "143"


def _types(events):
    return [e["type"] for e in events]


def test_event_sequence_nonstream():
    events = list(solve_stream("p", executor=_FakeExecutor(),
                               client=_FakeClient([ROUND1, ROUND2]), stream=False))
    assert _types(events) == ["reasoning_delta", "code", "tool_result",
                              "reasoning_delta", "final_answer"]
    final = events[-1]
    assert final["boxed"] == "143"
    assert "```output\n143\n```" in final["transcript"]
    assert final["truncated"] is False


def test_stream_and_nonstream_same_transcript():
    a = list(solve_stream("p", executor=_FakeExecutor(),
                          client=_FakeClient([ROUND1, ROUND2]), stream=False))[-1]
    b = list(solve_stream("p", executor=_FakeExecutor(),
                          client=_FakeClient([ROUND1, ROUND2]), stream=True))[-1]
    assert a["transcript"] == b["transcript"]
    assert a["boxed"] == b["boxed"] == "143"
    # streaming must emit more reasoning_delta events (chunked) than the single-shot path
    streamed = list(solve_stream("p", executor=_FakeExecutor(),
                                 client=_FakeClient([ROUND1, ROUND2]), stream=True))
    assert _types(streamed).count("reasoning_delta") > 2


def test_solve_wrapper_matches_streamed_transcript():
    final = list(solve_stream("p", executor=_FakeExecutor(),
                              client=_FakeClient([ROUND1, ROUND2]), stream=False))[-1]
    transcript = solve("p", executor=_FakeExecutor(), client=_FakeClient([ROUND1, ROUND2]))
    assert transcript == final["transcript"]
    assert extract_boxed(transcript) == "143"


def test_no_progress_guard_stops_repetition():
    # A model stuck re-emitting the same block (the GSM8K item-15 pathology) must stop after
    # one execution, not burn all max_calls rounds.
    REPEAT = "Try again:\n```python\nprint(1)\n```\n"

    class _RepeatCompletions:
        def create(self, stream=False, stream_options=None, **kw):
            if not stream:
                return _Completion(REPEAT)

            def gen():
                yield _Chunk(text=REPEAT, finish="stop")
                yield _Chunk(usage=_Usage())
            return gen()

    class _RepeatClient:
        completions = _RepeatCompletions()

    class _AnyExecutor:
        def run(self, code):
            return "1"

    events = list(solve_stream("p", executor=_AnyExecutor(), client=_RepeatClient(),
                               stream=False, max_calls=8))
    assert _types(events).count("tool_result") == 1, _types(events)  # ran once, then no-progress break
    assert events[-1]["type"] == "final_answer"


def test_error_becomes_event():
    class _Boom:
        def create(self, **kw):
            raise RuntimeError("boom 503")

    class _C:
        completions = _Boom()
    events = list(solve_stream("p", executor=_FakeExecutor(), client=_C(), stream=False))
    assert events[-1]["type"] == "error" and "boom 503" in events[-1]["message"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
