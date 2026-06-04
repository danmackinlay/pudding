"""grade() coverage: the integer fast-path (AIME/GSM8K/AMC) + MATH LaTeX equivalence."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval import grade, _HAS_MATH_VERIFY  # noqa: E402


def test_integer_grading():
    assert grade("143", "143")
    assert grade("72", "72")
    assert not grade("43", "143")          # the specialist's 7^999 miss


def test_exact_match_never_failed_by_grader():
    # the calibration bug: math_verify can't compare 'p - q' (free vars) and returned False;
    # an identical string must always grade True.
    assert grade("p - q", "p - q")
    assert grade("\\frac{3}{56}", "\\frac{3}{56}")


def test_none_is_wrong():
    assert not grade(None, "5")


def test_latex_equivalence():
    # math_verify handles the common MATH-500 forms; skip cleanly if the dep is absent.
    if not _HAS_MATH_VERIFY:
        return
    assert grade("\\frac{1}{2}", "0.5")
    assert grade("0.50", "1/2")
    assert not grade("\\frac{1}{3}", "0.5")
