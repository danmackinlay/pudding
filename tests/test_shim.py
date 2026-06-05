"""Offline tests for shim.py — the swappable workbench. No network, no kernel.

We monkeypatch `shim.stream_events` with a *fake async event stream*, so we exercise the picker,
the one renderer (FOIM buffer, fenced output, trust verdict, footer, thinking channel) and the
OpenAI SSE contract Open WebUI relies on — all headless (mirror tests/test_strategies.py's
fake-client style). Run: direnv exec . uv run python -m pytest tests/test_shim.py
                     or: direnv exec . uv run python tests/test_shim.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shim

_REAL_STREAM_EVENTS = shim.stream_events           # restore after a test fakes it


def _fake(events):
    """A drop-in for shim.stream_events that replays a scripted event list (ignores routing)."""
    async def gen(problem, entry):
        for e in events:
            yield e
    return gen


def _set_events(events):
    shim.stream_events = _fake(events)             # _render_pieces resolves this at call time


def _entry(**over):
    e = {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "strategy": "cot",
         "label": "DeepSeek-V4-Pro · CoT", "mode": "stream", "k": None}
    e.update(over)
    return e


def _pieces(events, entry=None):
    _set_events(events)
    entry = entry or _entry()

    async def run():
        return [p async for p in shim._render_pieces("q", entry)]
    return asyncio.run(run())


def _by_channel(pieces, channel):
    return "".join(text for ch, text in pieces if ch == channel)


def _client():
    from fastapi.testclient import TestClient
    return TestClient(shim.app)


# --- the picker ------------------------------------------------------------
def test_models_endpoint_lists_lineup_rungs():
    shim.stream_events = _REAL_STREAM_EVENTS
    ids = [m["id"] for m in _client().get("/v1/models").json()["data"]]
    assert ids, "picker is empty — contenders.jsonl didn't load"
    assert any(i.endswith("-cot") for i in ids), ids
    assert any(i.endswith("-deep") for i in ids), ids      # synthesized maj@k siblings
    assert any(i.endswith("-tir") for i in ids), ids       # the bridged specialist


# --- the renderer (headless) -----------------------------------------------
def test_foim_never_flashes_half_open_and_footer():
    # \boxed{143} split across two deltas: the buffer must hold it until the brace closes.
    pieces = _pieces([
        {"type": "reasoning_delta", "text": "The answer is \\boxed{14"},
        {"type": "reasoning_delta", "text": "3}."},
        {"type": "final_answer", "boxed": "143", "elapsed_s": 1.2, "completion_tokens": 21000,
         "truncated": False},
    ])
    contents = [t for ch, t in pieces if ch == "content"]
    joined = "".join(contents)
    assert "\\boxed{143}" in joined
    assert not any("\\boxed{14" in p and "\\boxed{143}" not in p for p in contents), \
        "FOIM flashed a half-open \\boxed{"
    assert "⏱ 1.2s" in joined and "21000 tok" in joined
    assert "DeepSeek-V4-Pro · CoT" in joined
    assert "~$0.0183" in joined          # 21000 × 0.87/1e6, from prices.json


def test_thinking_lands_on_the_reasoning_channel():
    pieces = _pieces([
        {"type": "thinking_delta", "text": "let me reason about this carefully… "},
        {"type": "reasoning_delta", "text": "The answer is \\boxed{9}."},
        {"type": "final_answer", "boxed": "9", "elapsed_s": 0.5, "completion_tokens": 30,
         "truncated": False},
    ])
    assert "let me reason" in _by_channel(pieces, "reasoning")
    content = _by_channel(pieces, "content")
    assert "\\boxed{9}" in content and "let me reason" not in content


def test_self_verify_verdict_confirmed_and_corrected():
    confirmed = _pieces([
        {"type": "final_answer", "boxed": "42", "candidate_boxed": "42", "elapsed_s": 1.0,
         "completion_tokens": 5, "truncated": False}],
        entry=_entry(strategy="self_verify", label="Kimi-K2.6 · self-verify",
                     model="moonshotai/kimi-k2.6"))
    assert "✓ self-checked: confirmed" in _by_channel(confirmed, "content")

    corrected = _pieces([
        {"type": "final_answer", "boxed": "43", "candidate_boxed": "41", "elapsed_s": 1.0,
         "completion_tokens": 5, "truncated": False}],
        entry=_entry(strategy="self_verify"))
    c = _by_channel(corrected, "content")
    assert "⚠ self-check corrected" in c and "\\(41\\)" in c and "\\(43\\)" in c


def test_deep_agreement_panel():
    pieces = _pieces([
        {"type": "status", "text": "⏳ running 5 samples (maj@5)…"},
        {"type": "final_answer", "boxed": "7", "agreement": 0.8, "k": 5,
         "completion_tokens": 1000, "elapsed_s": 3.0, "truncated": False}],
        entry=_entry(strategy="cot", mode="deep", k=5, label="DeepSeek-V4-Pro · maj@5"))
    c = _by_channel(pieces, "content")
    assert "running 5 samples" in c
    assert "**\\(7\\)**" in c and "agreement 4/5" in c     # 0.8 × 5 = 4


# --- the OpenAI SSE contract -----------------------------------------------
def _collect_stream(resp_text):
    content = reasoning = ""
    saw_done = saw_role = saw_finish = False
    for line in resp_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            saw_done = True
            continue
        delta = json.loads(payload)["choices"][0]["delta"]
        if delta.get("role") == "assistant":
            saw_role = True
        content += delta.get("content") or ""
        reasoning += delta.get("reasoning_content") or ""
        if json.loads(payload)["choices"][0]["finish_reason"] == "stop":
            saw_finish = True
    return content, reasoning, saw_role, saw_finish, saw_done


def test_streaming_sse_contract_and_rendering():
    _set_events([
        {"type": "reasoning_delta", "text": "Compute:\n```python\nprint(143)\n```\n"},
        {"type": "code", "lang": "python", "code": "print(143)"},   # structural — must NOT dup
        {"type": "tool_result", "output": "143"},
        {"type": "thinking_delta", "text": "checking the modulus…"},
        {"type": "reasoning_delta", "text": "So the answer is \\boxed{143}."},
        {"type": "final_answer", "boxed": "143", "elapsed_s": 1.2, "completion_tokens": 10,
         "truncated": False},
    ])
    resp = _client().post("/v1/chat/completions", json={
        "model": shim.DEFAULT_MODEL, "stream": True,
        "messages": [{"role": "user", "content": "7^999 mod 1000?"}]})
    assert resp.status_code == 200
    content, reasoning, saw_role, saw_finish, saw_done = _collect_stream(resp.text)
    assert saw_role and saw_finish and saw_done, "missing role/finish/[DONE] in SSE"
    assert content.count("```python") == 1, f"```python not shown exactly once:\n{content}"
    assert content.count("```output") == 1
    assert "143" in content and "\\boxed{143}" in content
    assert "⏱" in content and "10 tok" in content
    assert "checking the modulus" in reasoning      # thinking on the reasoning_content channel


def test_nonstream_completion():
    _set_events([
        {"type": "reasoning_delta", "text": "answer \\boxed{143}"},
        {"type": "final_answer", "boxed": "143", "elapsed_s": 1.0, "completion_tokens": 8,
         "truncated": False}])
    resp = _client().post("/v1/chat/completions", json={
        "model": shim.DEFAULT_MODEL, "stream": False,
        "messages": [{"role": "user", "content": "q"}]}).json()
    assert "\\boxed{143}" in resp["choices"][0]["message"]["content"]


def test_unknown_model_errors_gracefully():
    shim.stream_events = _REAL_STREAM_EVENTS         # exercise the real unknown-id branch
    resp = _client().post("/v1/chat/completions", json={
        "model": "no-such-model-xyz", "stream": False,
        "messages": [{"role": "user", "content": "q"}]}).json()
    assert "unknown model" in resp["choices"][0]["message"]["content"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
