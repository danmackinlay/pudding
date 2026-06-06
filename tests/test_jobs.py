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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
