"""Offline test for shim.py — fake solve_stream + fake Kernel, FastAPI TestClient.

Locks the OpenAI SSE contract Open WebUI relies on, and the rendering rules:
  - well-formed chat.completion.chunk SSE ending in [DONE],
  - the model's ```python block is shown ONCE (not duped by the code event),
  - the injected ```output appears, plus the time/token footer.
No network, no kernel. Run: direnv exec . uv run python -m pytest tests/test_shim.py
                       or: direnv exec . uv run python tests/test_shim.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shim


def _fake_solve_stream(problem, executor=None, model=None, stream=True):
    # The model's prose carries the ```python block (as real models do); the `code` event is
    # structural-only and must NOT be re-rendered.
    yield {"type": "reasoning_delta", "text": "Compute:\n```python\nprint(143)\n```\n"}
    yield {"type": "code", "lang": "python", "code": "print(143)"}
    yield {"type": "tool_result", "output": "143"}
    yield {"type": "reasoning_delta", "text": "So the answer is \\boxed{143}."}
    yield {"type": "final_answer", "boxed": "143", "transcript": "…",
           "elapsed_s": 1.2, "completion_tokens": 10, "truncated": False}


class _FakeKM:
    def shutdown_kernel(self, now=True):
        pass


class _FakeKernel:
    def __init__(self):
        self.km = _FakeKM()


def _client(monkeypatch=None):
    from fastapi.testclient import TestClient
    shim.solve_stream = _fake_solve_stream  # type: ignore[assignment]
    shim.Kernel = _FakeKernel               # type: ignore[assignment]
    return TestClient(shim.app)


def test_models_endpoint():
    c = _client()
    data = c.get("/v1/models").json()
    assert data["data"][0]["id"] == "tir-solver"


def _collect_stream(resp_text):
    content = ""
    saw_done = saw_role = saw_finish = False
    for line in resp_text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload.strip() == "[DONE]":
            saw_done = True
            continue
        body = json.loads(payload)
        delta = body["choices"][0]["delta"]
        if delta.get("role") == "assistant":
            saw_role = True
        if "content" in delta:
            content += delta["content"]
        if body["choices"][0]["finish_reason"] == "stop":
            saw_finish = True
    return content, saw_role, saw_finish, saw_done


def test_streaming_sse_contract_and_rendering():
    c = _client()
    resp = c.post("/v1/chat/completions", json={
        "model": "tir-solver", "stream": True,
        "messages": [{"role": "user", "content": "7^999 mod 1000?"}],
    })
    assert resp.status_code == 200
    content, saw_role, saw_finish, saw_done = _collect_stream(resp.text)
    assert saw_role and saw_finish and saw_done, "missing role/finish/[DONE] in SSE"
    # code block shown exactly once (dedup), output injected once, footer present
    assert content.count("```python") == 1, f"```python not shown exactly once:\n{content}"
    assert content.count("```output") == 1
    assert "143" in content and "\\boxed{143}" in content
    assert "⏱" in content and "10 tok" in content


def test_nonstream_completion():
    c = _client()
    resp = c.post("/v1/chat/completions", json={
        "model": "tir-solver", "stream": False,
        "messages": [{"role": "user", "content": "q"}],
    }).json()
    msg = resp["choices"][0]["message"]["content"]
    assert msg.count("```python") == 1 and "```output" in msg and "\\boxed{143}" in msg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
