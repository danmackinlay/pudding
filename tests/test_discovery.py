"""Offline tests for the generative loop (P3): conjecture → falsify → survivors.

The LLM proposal is network-free — we monkeypatch `discovery._propose_async` with scripted JSON,
so we exercise parsing, provenance, and the flock view. But the **falsification oracle is run for
real** (it's a local subprocess, not the network): a true claim survives, a false one is refuted
with a witness, a broken/missing harness errors, and a runaway search is killed by the timeout.
Run: direnv exec . uv run python tests/test_discovery.py   (or via import — see MEMORY).
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["PUDDING_JOBS_DIR"] = tempfile.mkdtemp(prefix="pudding-jobs-")   # before importing pudding

import pudding                            # noqa: E402
from pudding import discovery             # noqa: E402

# --- scripted candidates (a true claim, a false claim, a broken harness, no harness) -----------
_TRUE = "def counterexample():\n    return next((n for n in range(-50, 51) if (n*n - n) % 2), None)\n"
_FALSE = ("def counterexample():\n    import sympy\n"
          "    return next((n for n in range(60) if not sympy.isprime(n*n + n + 41)), None)\n")
_BROKEN = "def counterexample():\n    return undefined_name\n"

_CANDIDATES = [
    {"statement": r"For all integers $n$, $n^2 - n$ is even.", "rationale": "consecutive ints",
     "check": _TRUE},
    {"statement": r"$n^2 + n + 41$ is prime for every $n \ge 0$.", "rationale": "Euler",
     "check": _FALSE},
    {"statement": "A claim whose harness is broken.", "rationale": "—", "check": _BROKEN},
    {"statement": "A claim with no harness at all.", "rationale": "—", "check": ""},
]


def _fake_propose(candidates):
    async def _propose(context, *, n, model, provider, temperature, seed, max_tokens):
        return "```json\n" + json.dumps(candidates[:n]) + "\n```"
    return _propose


def _use(candidates):
    discovery._propose_async = _fake_propose(candidates)


# --- tests ---------------------------------------------------------------------------------------
def test_conjecture_parses_and_tags_provenance():
    _use(_CANDIDATES)
    flock = asyncio.run(pudding.conjecture("seq context", n=4, models=["deepseek-v4-pro"]))
    assert len(flock.conjectures) == 4
    assert [c.id for c in flock.conjectures] == ["c0", "c1", "c2", "c3"]
    assert all(c.origin == "deepseek-v4-pro" and c.status == "proposed" for c in flock.conjectures)
    assert "n^2 - n" in flock.conjectures[0].statement or "n^2" in flock.conjectures[0].statement


def test_falsify_oracle_separates_survivors_from_refuted():
    _use(_CANDIDATES)

    async def run():
        flock = await pudding.conjecture("ctx", n=4, models=["deepseek-v4-pro"])
        return await pudding.falsify(flock, timeout=8)

    flock = asyncio.run(run())
    by_id = {c.id: c for c in flock.conjectures}
    assert by_id["c0"].status == "survives"                       # n^2-n even — not refuted
    assert by_id["c1"].status == "refuted" and by_id["c1"].witness == "40"   # Euler fails at 40
    assert by_id["c2"].status == "error"                          # broken harness
    assert by_id["c3"].status == "error" and "no falsification harness" in by_id["c3"].detail
    assert [c.id for c in flock.survivors] == ["c0"]
    assert [c.id for c in flock.refuted] == ["c1"]


def test_discover_chains_conjecture_then_falsify():
    _use(_CANDIDATES)
    flock = asyncio.run(pudding.discover("ctx", n=4, models=["deepseek-v4-pro"], timeout=8))
    assert flock.counts() == {"proposed": 0, "survives": 1, "refuted": 1, "error": 2}
    assert [s.statement for s in flock.survivors] == [flock.conjectures[0].statement]


def test_timeout_kills_a_runaway_harness():
    slow = [{"statement": "runaway", "rationale": "—",
             "check": "def counterexample():\n    while True:\n        pass\n"}]
    _use(slow)

    async def run():
        flock = await pudding.conjecture("ctx", n=1, models=["deepseek-v4-pro"])
        return await pudding.falsify(flock, timeout=1.0)

    flock = asyncio.run(asyncio.wait_for(run(), timeout=6))        # must be fast, not hang
    assert flock.conjectures[0].status == "error"
    assert "timeout" in flock.conjectures[0].detail


def test_parse_tolerates_bare_array_without_fence():
    bare = [{"statement": "x", "rationale": "y", "check": "def counterexample():\n    return None\n"}]

    async def _propose(context, *, n, model, provider, temperature, seed, max_tokens):
        return "here you go:\n" + json.dumps(bare) + "\nhope that helps"
    discovery._propose_async = _propose
    flock = asyncio.run(pudding.discover("ctx", n=1, models=["deepseek-v4-pro"], timeout=8))
    assert len(flock.conjectures) == 1 and flock.conjectures[0].status == "survives"


def test_parse_garbage_yields_empty_flock_not_crash():
    async def _propose(context, *, n, model, provider, temperature, seed, max_tokens):
        return "I cannot help with that."
    discovery._propose_async = _propose
    flock = asyncio.run(pudding.conjecture("ctx", n=3, models=["deepseek-v4-pro"]))
    assert flock.conjectures == [] and "0 proposed" in flock.markdown


def test_flock_markdown_and_view_model_are_honest():
    _use(_CANDIDATES)
    flock = asyncio.run(pudding.discover("ctx", n=4, models=["deepseek-v4-pro"], timeout=8))
    md = flock.markdown
    assert "Surviving ≠ proven" in md                              # the honesty rail
    assert "candidates worth proving" in md and "counterexample `40`" in md
    vm = pudding.flock_view_model(flock)
    assert vm["counts"]["survives"] == 1 and vm["counts"]["refuted"] == 1
    assert vm["survivors"] == [flock.conjectures[0].statement]
    rows = {r["id"]: r for r in vm["conjectures"]}
    assert rows["c1"]["badge"].startswith("❌") and rows["c0"]["badge"].startswith("✅")


def test_on_event_sink_observes_proposals_and_verdicts():
    _use(_CANDIDATES)
    seen = []

    async def run():
        flock = await pudding.conjecture("ctx", n=4, models=["deepseek-v4-pro"], on_event=seen.append)
        return await pudding.falsify(flock, timeout=8, on_event=seen.append)

    asyncio.run(run())
    assert sum(e["type"] == "conjecture" for e in seen) == 4
    verdicts = [e for e in seen if e["type"] == "verdict"]
    assert len(verdicts) == 4 and any(e["status"] == "refuted" and e["witness"] == "40" for e in verdicts)


def test_flock_roundtrips_through_dict():
    _use(_CANDIDATES)
    flock = asyncio.run(pudding.discover("ctx", n=4, models=["deepseek-v4-pro"], timeout=8))
    back = discovery.flock_from_dict(discovery.flock_to_dict(flock))
    assert [c.status for c in back.conjectures] == [c.status for c in flock.conjectures]
    assert [s.id for s in back.survivors] == [s.id for s in flock.survivors]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
