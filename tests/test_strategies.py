"""Unit tests for the generalist strategies (cot / self_verify) and the strategy dispatch.

No network: a FakeClient returns scripted chat completions, so we exercise the envelope, the
reasoning_content fallback, and the solve()→solve_stream()→generalist_stream() routing offline.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from solver_loop import solve, solve_one, extract_boxed          # noqa: E402
from strategies import cot_stream, self_verify_stream, generalist_stream  # noqa: E402


# --- a scripted, network-free chat client ----------------------------------
# Scripted items are (content, reasoning) or (content, reasoning, reasoning_key) — the third
# element lets a test put the trace under OpenRouter's `reasoning` instead of `reasoning_content`.
class _Msg:
    def __init__(self, content, reasoning=None, reasoning_key="reasoning_content"):
        self.content = content
        if reasoning is not None:
            setattr(self, reasoning_key, reasoning)


class _Choice:
    def __init__(self, content, reasoning=None, finish="stop", reasoning_key="reasoning_content"):
        self.message = _Msg(content, reasoning, reasoning_key)
        self.finish_reason = finish


class _Usage:
    def __init__(self):
        self.completion_tokens = 42
        self.prompt_tokens = 7


class _Resp:
    def __init__(self, content, reasoning=None, reasoning_key="reasoning_content"):
        self.choices = [_Choice(content, reasoning, reasoning_key=reasoning_key)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, scripted):
        self._scripted = list(scripted)

    def create(self, **kw):
        item = self._scripted.pop(0)
        key = item[2] if len(item) > 2 else "reasoning_content"
        return _Resp(item[0], item[1], key)


class FakeClient:
    """Mimic the slice of the OpenAI client the strategies touch: chat.completions.create."""
    def __init__(self, scripted):
        self.chat = type("C", (), {"completions": _Completions(scripted)})()


def _final(events):
    return next(e for e in events if e["type"] == "final_answer")


# --- cot -------------------------------------------------------------------
def test_cot_reads_boxed_and_usage():
    client = FakeClient([("Reason… therefore \\boxed{42}.", None)])
    evts = list(cot_stream("q", client=client, model="m", temperature=0.0,
                           seed=None, max_tokens=100, stream=False))
    f = _final(evts)
    assert f["boxed"] == "42"
    assert f["completion_tokens"] == 42
    assert f["truncated"] is False


def test_cot_falls_back_to_reasoning_content_when_content_empty():
    # "thinking ate the budget": answer only appears in reasoning_content.
    client = FakeClient([("", "the work… \\boxed{7}")])
    evts = list(cot_stream("q", client=client, model="m", temperature=0.0,
                           seed=None, max_tokens=100, stream=False))
    assert _final(evts)["boxed"] == "7"


# --- self_verify -----------------------------------------------------------
def test_self_verify_uses_the_corrected_answer():
    client = FakeClient([("Try \\boxed{41}.", None),
                         ("That's off by two; correct is \\boxed{43}.", None)])
    evts = list(self_verify_stream("q", client=client, model="m", temperature=0.0,
                                   seed=1, max_tokens=100, stream=False))
    f = _final(evts)
    assert f["boxed"] == "43"
    assert f["completion_tokens"] == 84      # both passes summed


# --- dispatch --------------------------------------------------------------
def test_solve_routes_through_cot_strategy():
    client = FakeClient([("Answer: \\boxed{99}", None)])
    transcript = solve("q", strategy="cot", model="m", client=client)
    assert extract_boxed(transcript) == "99"


def test_generalist_requires_a_model():
    evts = list(generalist_stream("q", strategy="cot", model=None))
    assert evts[-1]["type"] == "error" and "model" in evts[-1]["message"]


def test_tools_rung_is_gated():
    evts = list(generalist_stream("q", strategy="tools", model="m", client=FakeClient([])))
    assert evts[-1]["type"] == "error" and "tools" in evts[-1]["message"].lower()


def test_cot_handles_openrouter_reasoning_key():
    # OpenRouter exposes the trace as `reasoning`; empty content forces the \boxed{} fallback.
    client = FakeClient([("", "work… \\boxed{5}", "reasoning")])
    evts = list(cot_stream("q", client=client, model="m", temperature=0.0,
                           seed=None, max_tokens=100, stream=False))
    assert _final(evts)["boxed"] == "5"


def test_solve_one_reports_boxed_and_tokens():
    client = FakeClient([("the answer is \\boxed{12}", None)])
    r = solve_one("q", strategy="cot", model="m", client=client)
    assert r["boxed"] == "12"
    assert r["completion_tokens"] == 42
    assert r["error"] is None
