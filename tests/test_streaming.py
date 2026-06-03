"""Offline tests for streaming.py — the FOIM delimiter buffer + normalizer.

No network, no kernel, no GPU. Run either:
    direnv exec . uv run python -m pytest tests/test_streaming.py
    direnv exec . uv run python tests/test_streaming.py     # plain-assert fallback
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from streaming import LatexSafeBuffer, latex_safe_stream, normalize_delimiters


def _split_roundtrip(chunks):
    """Feed chunks through the buffer; return (outputs_list, joined). Asserts roundtrip."""
    b = LatexSafeBuffer()
    outs = [b.feed(c) for c in chunks]
    outs.append(b.flush())
    joined = "".join(outs)
    assert joined == "".join(chunks), f"roundtrip lost data: {joined!r}"
    return outs, joined


def test_boxed_held_until_closed():
    b = LatexSafeBuffer()
    first = b.feed("answer is \\boxed{14")
    assert "\\boxed" not in first, f"leaked partial \\boxed: {first!r}"
    rest = b.feed("3} done")
    assert "\\boxed{143}" in (first + rest)


def test_inline_dollar_held_until_closed():
    b = LatexSafeBuffer()
    a = b.feed("x is $a^2")
    assert "$" not in a, f"leaked partial inline math: {a!r}"
    bb = b.feed("+b$ ok")
    assert "$a^2+b$" in (a + bb)


def test_code_fence_held_until_closed():
    b = LatexSafeBuffer()
    c = b.feed("```python\nprint(1)\n")
    assert "```python" not in c, f"leaked partial fence: {c!r}"
    d = b.feed("```\n")
    assert "```python\nprint(1)\n```" in (c + d)


def test_stray_dollar_newline_does_not_wedge():
    # A lone '$' (e.g. "$5") followed by a newline must flush, not hold forever.
    b = LatexSafeBuffer()
    out = b.feed("cost is $5\nnext line") + b.flush()
    assert "next line" in out


def test_display_math_char_by_char():
    _split_roundtrip(list("see $$\\frac{1}{2}$$ end"))


def test_paren_and_bracket_split():
    _split_roundtrip(["the \\(x", "^2\\) and \\[y", "=1\\] z"])


def test_escaped_dollar_is_literal():
    b = LatexSafeBuffer()
    out = b.feed("price \\$5 each ")
    assert "\\$5" in out, f"escaped dollar mishandled: {out!r}"


def test_latex_safe_stream_generator():
    chunks = ["the answer ", "is \\boxed{", "42} for ", "$x$ here"]
    out = "".join(latex_safe_stream(chunks))
    assert out == "".join(chunks)


def test_normalize_inline_dollar_to_paren():
    assert normalize_delimiters("val $x^2$ here") == "val \\(x^2\\) here"


def test_normalize_leaves_display_and_paren_untouched():
    assert normalize_delimiters("disp $$y=1$$ ok") == "disp $$y=1$$ ok"
    assert normalize_delimiters("keep \\(z\\) and \\[w\\]") == "keep \\(z\\) and \\[w\\]"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"OK  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
