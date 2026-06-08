"""Offline unit tests for the prover engine (pudding/prove.py).

No network, no Modal: a scripted AsyncFakeClient (reused from test_strategies) feeds the
generalist's Lean output, and a FakeVerifier stands in for the Kimina compiler. Covers fence
extraction, the anti-laundering signature guard, the generate→verify→self-correct loop, and the
Pass@k reducer (shortest compiling proof; honest "no candidate compiled" render). The networked
end-to-end path (real OpenRouter + Modal Lean server) is exercised by the Q1 DoD, not here.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root (import pudding, engine)
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests/ (reuse AsyncFakeClient)

from test_strategies import AsyncFakeClient                                          # noqa: E402
from pudding.prove import (extract_proof, _preserves_signature, prove_one_async,     # noqa: E402
                           reduce_proofs, prove)
from pudding.jobs import Attempt                                                      # noqa: E402

STMT = "import Mathlib\n\ntheorem t (a b : ℝ) : a + b = b + a := by sorry"
FAITHFUL = "import Mathlib\n\ntheorem t (a b : ℝ) : a + b = b + a := by ring"
ALTERED = "import Mathlib\n\ntheorem t (a b : ℝ) : a + b = a + b := by rfl"     # weakened statement


def _reply(lean_file: str) -> str:
    return f"Here is the proof.\n```lean4\n{lean_file}\n```"


class FakeVerifier:
    """Stand-in for the Modal LeanVerifier: returns scripted verdicts, then defaults to ok."""

    def __init__(self, verdicts=None):
        self._v = list(verdicts or [])
        self.calls = 0

    def run(self, proof, timeout=60, add_header=False):
        self.calls += 1
        return self._v.pop(0) if self._v else {"ok": True, "errors": [], "sorries": [], "messages": []}


def _run(coro):
    return asyncio.run(coro)


# --- pure helpers ----------------------------------------------------------
def test_extract_proof_takes_last_fence():
    assert extract_proof("```lean4\nA\n```\nthen\n```lean4\nB\n```") == "B"
    assert extract_proof("no fence here") is None


def test_signature_guard_accepts_faithful_rejects_altered():
    assert _preserves_signature(STMT, FAITHFUL)                       # only the proof differs
    assert not _preserves_signature(STMT, ALTERED)                    # statement was weakened → caught
    assert _preserves_signature(STMT, FAITHFUL.replace("a + b = b + a", "a+b = b+a"))  # reformat-robust


# --- the engine: generate → verify → self-correct --------------------------
def test_prove_one_async_verifies_a_faithful_proof():
    r = _run(prove_one_async(STMT, client=AsyncFakeClient([(_reply(FAITHFUL), None)]),
                             verifier=FakeVerifier(), max_rounds=0))
    assert r["ok"] and r["proof"] == FAITHFUL and r["boxed"] == FAITHFUL
    assert r["error"] is None and r["rounds"] == 0


def test_prove_one_async_self_corrects_on_compiler_error():
    client = AsyncFakeClient([(_reply(FAITHFUL), None), (_reply(FAITHFUL), None)])
    verifier = FakeVerifier([{"ok": False, "errors": [{"data": "unsolved goals"}],
                              "sorries": [], "messages": []}])
    r = _run(prove_one_async(STMT, client=client, verifier=verifier, max_rounds=1))
    assert r["ok"] and r["rounds"] == 1 and verifier.calls == 2     # rejected once, then accepted


def test_prove_one_async_refuses_statement_drift():
    # Even though the verifier WOULD say ok, the guard blocks an altered statement pre-compile.
    verifier = FakeVerifier()
    r = _run(prove_one_async(STMT, client=AsyncFakeClient([(_reply(ALTERED), None)]),
                             verifier=verifier, max_rounds=0))
    assert not r["ok"] and verifier.calls == 0                       # never reached the compiler
    assert any("altered" in str(e.get("data", "")) for e in r["lean_errors"])


def test_prove_one_async_no_fence_is_a_clean_miss():
    r = _run(prove_one_async(STMT, client=AsyncFakeClient([("I cannot find a proof.", None)]),
                             verifier=FakeVerifier(), max_rounds=0))
    assert not r["ok"] and r["error"] is None and r["boxed"] is None


# --- the reducer: Pass@k = the shortest compiling proof --------------------
def _att(boxed, seed=0, error=None):
    return Attempt(model="m", provider="openrouter", model_id="x", seed=seed, boxed=boxed,
                   transcript="", tokens=10, ttft_s=None, decode_tok_s=None, truncated=False,
                   error=error, thinking="")


SPEC = {"k": 4, "model_names": ["m"], "problem": STMT, "strategy": "prove"}


def test_reduce_proofs_picks_shortest_compiling():
    r = reduce_proofs([_att("a longer proof body"), _att(None), _att("short"), _att(None)], SPEC)
    assert r.answer == "short" and r.count == 2 and r.n_total == 4 and r.agreement == 0.5
    assert "proof verified" in r.markdown and "2/4" in r.markdown


def test_reduce_proofs_no_compile_is_honest_miss():
    r = reduce_proofs([_att(None), _att(None, seed=1)], SPEC)
    assert r.answer is None and r.count == 0
    assert "no candidate compiled" in r.markdown


# --- the public verb builds a strategy=='prove' Job ------------------------
def test_prove_verb_builds_a_prove_job():
    job = prove(STMT, k=3, model="anthropic/claude-opus-4.5")
    assert job.spec["strategy"] == "prove" and job.spec["k"] == 3
    assert len(job.lanes) == 3 and all(lane.strategy == "prove" for lane in job.lanes)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
