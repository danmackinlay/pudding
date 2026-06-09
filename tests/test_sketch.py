"""Offline unit tests for the draft-sketch-prove harness (pudding/sketch.py).

No network/Modal: a scripted AsyncFakeClient feeds the decomposition reply, a FakeVerifier stands in
for Kimina. Covers leaf parsing, the theorem-not-punted check, skeleton validity (errors vs the
*expected* lemma sorries), reassembly splicing, and the glass-box render. The networked end-to-end —
real Opus drafting + self-correcting the combine, Modal Lean server, closing leaves — is the live
DoD, not here.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_strategies import AsyncFakeClient                                         # noqa: E402
from pudding.sketch import (parse_leaves, _theorem_is_sorry, reassemble, check_skeleton,  # noqa: E402
                            draft_sketch, render_sketch, Sketch)

SKEL = """import Mathlib

lemma h1 (n : ℤ) : n ^ 3 - n = (n - 1) * n * (n + 1) := by sorry
lemma h2 (n : ℤ) : (2 : ℤ) ∣ (n - 1) * n * (n + 1) := by sorry

theorem div6 (n : ℤ) : (6 : ℤ) ∣ n ^ 3 - n := by
  rw [h1]; exact combine h2"""


class FakeVerifier:
    def __init__(self, verdicts=None):
        self._v = list(verdicts or [])

    def run(self, proof, timeout=60, add_header=False):
        return self._v.pop(0) if self._v else {"ok": True, "errors": [], "sorries": [], "messages": []}


def _run(c):
    return asyncio.run(c)


def test_parse_leaves_takes_lemmas_not_theorem():
    assert [lf.name for lf in parse_leaves(SKEL)] == ["h1", "h2"]    # theorem combines, not a hole


def test_theorem_is_sorry():
    assert not _theorem_is_sorry(SKEL)
    assert _theorem_is_sorry("theorem t : True := by sorry")


def test_reassemble_splices_closed_leaves():
    leaves = parse_leaves(SKEL)
    leaves[0].ok = True
    leaves[0].proof = "lemma h1 (n : ℤ) : n ^ 3 - n = (n - 1) * n * (n + 1) := by ring"
    out = reassemble(SKEL, leaves)
    assert "by ring" in out
    assert "n ^ 3 - n = (n - 1) * n * (n + 1) := by sorry" not in out      # h1 swapped
    assert "lemma h2" in out and ":= by sorry" in out                       # h2 still open


def test_check_skeleton_valid_when_only_sorries():
    # the lemma `sorry`s are EXPECTED — only *errors* invalidate the plan
    v = FakeVerifier([{"ok": False, "errors": [], "sorries": [{"x": 1}], "messages": []}])
    ok, errs = _run(check_skeleton(SKEL, verifier=v, timeout=10))
    assert ok and errs == []


def test_check_skeleton_invalid_when_errors():
    v = FakeVerifier([{"ok": False, "errors": [{"data": "type mismatch"}], "sorries": [], "messages": []}])
    ok, errs = _run(check_skeleton(SKEL, verifier=v, timeout=10))
    assert not ok and errs


def test_draft_sketch_extracts_and_prepends_import():
    reply = ("Plan: factor.\n```lean\nlemma h (n:ℤ): True := by sorry\n"
             "theorem t (n:ℤ): True := by exact h n\n```")        # ```lean (not lean4), no import
    client = AsyncFakeClient([(reply, None)])
    _raw, skel = _run(draft_sketch("theorem t (n:ℤ): True := by sorry",
                                   model="m", temperature=0.0, seed=0, max_tokens=100, client=client))
    assert skel.startswith("import Mathlib")           # prepended (the fence had none)
    assert "lemma h" in skel and "theorem t" in skel


def test_render_invalid_plan_explains_and_stops():
    s = Sketch(target="theorem t : P", skeleton=SKEL, skeleton_ok=False,
               skeleton_errors=[{"data": "combine type mismatch"}], leaves=parse_leaves(SKEL))
    md = render_sketch(s)
    assert "INVALID plan" in md and "type mismatch" in md and "before spending on leaves" in md


def test_render_verified_shows_whole_proof():
    leaves = parse_leaves(SKEL)
    for lf in leaves:
        lf.ok = True
    s = Sketch(target="theorem t : P", skeleton=SKEL, skeleton_ok=True, leaves=leaves,
               reassembled="import Mathlib\n…whole proof…", final_ok=True)
    md = render_sketch(s)
    assert "whole proof VERIFIED" in md and "2/2 closed" in md


def test_render_partial_is_honest():
    leaves = parse_leaves(SKEL)
    leaves[0].ok = True
    s = Sketch(target="theorem t : P", skeleton=SKEL, skeleton_ok=True, leaves=leaves, final_ok=False)
    md = render_sketch(s)
    assert "incomplete" in md and "1/2" in md and "not a laundered" in md


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
