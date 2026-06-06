"""Offline tests for the pudding library / job layer (P1). No network, no kernel.

We monkeypatch `pudding.jobs.solve_one_async` with a scripted fake (model→answer), so we exercise
the fan-out, the keep-every-sample collector, clustering + within/cross-model agreement, the
markdown artifact, persistence round-trip, and both the sync (.result()) and async (await) paths.
Run: direnv exec . uv run python tests/test_jobs.py   (or via import — see MEMORY).
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PUDDING_JOBS_DIR"] = tempfile.mkdtemp(prefix="pudding-jobs-")   # before importing pudding

import pudding                       # noqa: E402
from pudding import api, jobs        # noqa: E402


# --- scripted, network-free engine -----------------------------------------
async def _fake_solve_one_async(problem, *, strategy, model, provider, temperature, seed,
                                max_tokens, **kw):
    # DeepSeek and Qwen agree on 144; Kimi dissents with 142 — gives clusters + cross-model.
    ans = "144" if ("deepseek" in model or "qwen" in model) else "142"
    return {"boxed": ans, "transcript": f"reason…\\boxed{{{ans}}}", "completion_tokens": 100,
            "truncated": False, "elapsed_s": 0.1, "ttft_s": 0.1, "decode_tok_s": 50.0,
            "error": None}


async def _fake_no_answer(problem, *, strategy, model, provider, temperature, seed, max_tokens, **kw):
    return {"boxed": None, "transcript": "", "completion_tokens": 10, "truncated": False,
            "elapsed_s": 0.1, "ttft_s": None, "decode_tok_s": None, "error": None}


def _use(fake):
    jobs.solve_one_async = fake      # _collect resolves the module global at call time


_MODELS = ["deepseek-v4-pro", "qwen3-7-max", "kimi-k2-6"]


# --- tests -----------------------------------------------------------------
def test_clusters_agreement_and_cross_model():
    _use(_fake_solve_one_async)
    res = api.solve("q", k=3, models=_MODELS).result()      # sync path (no running loop)
    assert res.answer == "144"
    assert res.n_total == 9 and res.n_answered == 9
    assert res.count == 6                                    # deepseek(3) + qwen(3)
    assert res.cross_model is True                           # two distinct models agree
    assert {c.answer for c in res.clusters} == {"144", "142"}
    assert "$144$" in res.markdown and "agreement 6/9" in res.markdown and "cross-model" in res.markdown


def test_keeps_every_sample_with_transcripts():
    _use(_fake_solve_one_async)
    res = api.solve("q", k=4, models=["deepseek-v4-pro"]).result()
    assert len(res.attempts) == 4                            # nothing discarded by the vote
    assert all(a.transcript and a.boxed == "144" for a in res.attempts)
    assert sorted(a.seed for a in res.attempts) == [0, 1, 2, 3]


def test_no_answer_cluster():
    _use(_fake_no_answer)
    res = api.solve("q", k=2, models=["deepseek-v4-pro"]).result()
    assert res.answer is None and res.n_answered == 0 and res.clusters == []
    assert "No answer" in res.markdown


def test_store_roundtrip():
    _use(_fake_solve_one_async)
    job = api.solve("q", k=2, models=["deepseek-v4-pro"])
    res = job.result()
    loaded = api.get(job.id)
    assert loaded is not None and loaded.status == "done"
    assert loaded._result.answer == res.answer == "144"
    assert len(loaded.attempts) == 2
    assert api.get("nonexistent-id") is None


def test_async_path_and_on_event_and_stream():
    _use(_fake_solve_one_async)
    seen = []

    async def run():
        job = api.solve("q", k=2, models=["deepseek-v4-pro"], on_event=seen.append)
        assert job.status == "running"                       # scheduled on the running loop
        res = await job
        return res

    res = asyncio.run(run())
    assert res.answer == "144"
    assert any(e["type"] == "attempt" for e in seen) and any(e["type"] == "done" for e in seen)


def test_stream_yields_then_completes():
    _use(_fake_solve_one_async)

    async def run():
        job = api.solve("q", k=3, models=["deepseek-v4-pro"])
        evs = [e async for e in job.stream()]
        return job, evs

    job, evs = asyncio.run(run())
    assert sum(e["type"] == "attempt" for e in evs) == 3
    assert evs[-1]["type"] == "done"
    assert job._result.answer == "144"


def test_render_owui_adapter():
    md = "**$144$** done"
    assert pudding.render(md, target="owui") == "**\\(144\\)** done"
    assert pudding.render(md, target="plain") == md


def test_modal_backend_is_deferred():
    try:
        api.solve("q", backend="modal")
        assert False, "expected NotImplementedError"
    except NotImplementedError as e:
        assert "modal" in str(e).lower()


def test_pin_freezes_content_addressed_and_reloads():
    _use(_fake_solve_one_async)
    res = api.solve("q", k=2, models=["deepseek-v4-pro"]).result()
    pinned = pudding.pin(res)
    assert pinned.pin and len(pinned.pin) == 12
    assert pudding.pin(res).pin == pinned.pin            # content-addressed → deterministic id
    loaded = pudding.get_pin(pinned.pin)
    assert loaded is not None and loaded.answer == "144"
    assert loaded.markdown == res.markdown               # re-renders identically
    assert pudding.get_pin("deadbeefcafe") is None


def test_widen_adds_samples_and_reclusters():
    _use(_fake_solve_one_async)

    async def run():
        job = api.solve("q", k=2, models=["deepseek-v4-pro", "kimi-k2-6"])
        r1 = await job
        assert r1.n_total == 4 and r1.k == 2
        return await job.widen(2)                        # +2 per model

    r2 = asyncio.run(run())
    assert r2.n_total == 8 and r2.k == 4
    assert sorted({a.seed for a in r2.attempts}) == [0, 1, 2, 3]   # new seeds, no collision
    assert "maj@4" in r2.markdown


def test_cancel_stops_inflight_fanout():
    # The kill-switch must cancel the CHILD tasks (close in-flight HTTP), not just orphan them.
    state = {"started": 0, "cancelled": 0, "finished": 0}

    async def _slow(problem, *, strategy, model, provider, temperature, seed, max_tokens, **kw):
        state["started"] += 1
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise
        state["finished"] += 1
        return {"boxed": "1", "transcript": "", "completion_tokens": 0, "truncated": False,
                "ttft_s": None, "decode_tok_s": None, "error": None}

    jobs.solve_one_async = _slow

    async def run():
        job = api.solve("q", k=4, models=["deepseek-v4-pro"])
        await asyncio.sleep(0.05)                       # let the children start
        assert state["started"] >= 1
        job.cancel()
        try:
            await asyncio.wait_for(job, timeout=2)      # must return fast, not after sleep(30)
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        return job

    job = asyncio.run(run())
    assert job.status == "cancelled"
    assert state["finished"] == 0                       # nothing completed
    assert state["cancelled"] >= 1                      # children were actually cancelled (no orphan)


def test_view_model_and_html():
    _use(_fake_solve_one_async)
    res = api.solve("q", k=2, models=["deepseek-v4-pro", "kimi-k2-6"]).result()
    vm = pudding.view_model(res)
    assert vm["answer"] == "144" and vm["agreement"] == "2/4"
    assert {c["answer"] for c in vm["clusters"]} == {"144", "142"}
    assert len(vm["attempts"]) == 4
    html = pudding.to_html(res)
    assert "144" in html and "<table>" in html and "<details>" in html


def test_thinking_captured_per_attempt():
    async def _think(problem, *, strategy, model, provider, temperature, seed, max_tokens, **kw):
        return {"boxed": "9", "transcript": "answer 9", "thinking": "first add the parts…",
                "completion_tokens": 50, "truncated": False, "ttft_s": 0.1, "decode_tok_s": 1.0,
                "error": None}
    _use(_think)
    res = api.solve("q", k=2, models=["deepseek-v4-pro"]).result()
    assert all(a.thinking == "first add the parts…" for a in res.attempts)   # the CoT is kept
    assert all(at["thinking"] for at in pudding.view_model(res)["attempts"])


def test_all_errored_headline_is_honest():
    async def _err(problem, *, strategy, model, provider, temperature, seed, max_tokens, **kw):
        return {"boxed": None, "transcript": "", "thinking": "", "completion_tokens": 0,
                "truncated": False, "ttft_s": None, "decode_tok_s": None,
                "error": "APIConnectionError: Connection error."}
    _use(_err)
    res = api.solve("q", k=2, models=["deepseek-v4-pro"]).result()
    assert res.answer is None
    md = res.markdown
    assert "failed" in md and "errored" in md and "Connection error" in md   # not a bland "No answer"


def test_recent_lists_summaries_newest_first():
    _use(_fake_solve_one_async)
    j1 = api.solve("alpha problem", k=1, models=["deepseek-v4-pro"]); j1.result()
    j2 = api.solve("beta problem", k=1, models=["deepseek-v4-pro"]); j2.result()
    rec = api.recent(50)
    ids = [s["id"] for s in rec]
    assert j1.id in ids and j2.id in ids
    assert ids.index(j2.id) < ids.index(j1.id)          # newest first
    s = next(s for s in rec if s["id"] == j2.id)
    assert s["problem"].startswith("beta") and s["answer"] == "144"


def test_timeout_caps_each_attempt():
    async def _slow(problem, *, strategy, model, provider, temperature, seed, max_tokens, **kw):
        await asyncio.sleep(30)                            # would hang without the cap
        return {"boxed": "1", "transcript": "", "thinking": "", "completion_tokens": 0,
                "truncated": False, "ttft_s": None, "decode_tok_s": None, "error": None}
    _use(_slow)

    async def run():
        return await api.solve("q", k=2, models=["deepseek-v4-pro"], timeout=0.2)

    res = asyncio.run(asyncio.wait_for(run(), timeout=3))   # must be fast, not 30s
    assert res.answer is None
    assert all("timeout" in (a.error or "") for a in res.attempts)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
