"""Frontend-agnostic streaming helpers: the shared event envelope, a delimiter-balancing
LaTeX buffer (the "flash of incomplete markdown" fix), and a delimiter normalizer.

All pure `str -> str` / generator logic — **no network, no Gradio, no FastAPI** — so the
loops, the OpenAI-compat shim (shim.py), the stage-2 prover UI, and the unit tests can all
import it headless.

Why this exists (PLAN.md §5.5): when a transcript streams token-by-token, a renderer that
sees a half-open delimiter (`\\boxed{14`, `$x^2`, an unclosed ```` ``` ```` fence) flashes raw
text until the delimiter closes. The fix is to hold output at a potentially-open delimiter
and only flush up to a balanced boundary — done here, server-side, so any frontend (Open
WebUI, a future Gradio prover panel) only ever receives complete delimiters.
"""
import re
from typing import Iterable, Iterator

# --- shared event envelope -------------------------------------------------
# Events are plain dicts {"type": <str>, ...payload} so they serialize trivially and match
# the repo's dict-return style. The solver emits the first group; the prover (stage 2) will
# emit the second, reusing reasoning_delta/final_answer. Consumers dispatch on "type" only,
# so adding the prover types never touches solver-side code.
#
# Solver:
#   {"type": "reasoning_delta", "text": str}         # a visible-content token chunk (pre-FOIM)
#   {"type": "thinking_delta",  "text": str}         # a hidden reasoning_content chunk (collapsible)
#   {"type": "code",           "lang": str, "code": str}
#   {"type": "tool_result",    "output": str}
#   {"type": "final_answer",   "boxed": str|None, "transcript": str, ...}
#        # optional trust fields, additive: candidate_boxed (self_verify pass-1), agreement+k (maj@k)
#   {"type": "error",          "message": str}
# Prover (reserved, stage 2):
#   {"type": "proof",      "lang": "lean4", "code": str}
#   {"type": "goal_state", "sorries": list, "messages": list}
#   {"type": "verdict",    "ok": bool, "round": int, "errors": list}


def ev(type_: str, **payload) -> dict:
    """Build an event dict. Tiny helper so call sites read `ev("code", lang=..., code=...)`."""
    return {"type": type_, **payload}


# --- FOIM: delimiter-balancing buffer --------------------------------------
# Parser states. `pending` always begins in TEXT (we only ever hold back from a delimiter
# opener onward; everything before it is flushed, so the remainder is re-scanned from TEXT).
_TEXT, _INLINE, _DISPLAY, _PAREN, _BRACKET, _MACRO, _CODE = range(7)


class LatexSafeBuffer:
    """Hold streamed text at potentially-open math/code delimiters; flush balanced prefixes.

    Usage:
        buf = LatexSafeBuffer()
        out = buf.feed(chunk)   # safe-to-render prefix (may be "")
        ...
        tail = buf.flush()      # force-emit the remainder at end of stream

    Balances: ``$…$``, ``$$…$$``, ``\\(…\\)``, ``\\[…\\]``, ``\\macro{…}`` (nested braces —
    e.g. ``\\boxed{``, ``\\frac{…}{…}``), and ```` ```…``` ```` fences. A newline inside inline
    ``$…$`` is treated as a safe flush (inline math rarely spans lines, and a stray ``$``
    would otherwise wedge the buffer forever). ``\\$`` and ``\\\\`` are handled as escapes.
    """

    def __init__(self):
        self.pending = ""

    def feed(self, text: str) -> str:
        self.pending += text
        safe_end = self._scan()
        out, self.pending = self.pending[:safe_end], self.pending[safe_end:]
        return out

    def flush(self) -> str:
        out, self.pending = self.pending, ""
        return out

    def _scan(self) -> int:
        """Return the length of the largest prefix of `pending` safe to emit (scan from TEXT)."""
        buf = self.pending
        n = len(buf)
        state, depth = _TEXT, 0
        i = safe_end = 0
        while i < n:
            if state == _TEXT:
                two, three = buf[i:i + 2], buf[i:i + 3]
                if three == "```":
                    state = _CODE; i += 3
                elif two == "$$":
                    state = _DISPLAY; i += 2
                elif buf[i] == "$":
                    if i + 1 >= n:          # lone trailing '$' — could become '$$'; hold
                        break
                    state = _INLINE; i += 1
                elif two == "\\(":
                    state = _PAREN; i += 2
                elif two == "\\[":
                    state = _BRACKET; i += 2
                elif buf[i] == "\\":
                    if i + 1 >= n:          # trailing backslash — could start \( \[ \macro; hold
                        break
                    nxt = buf[i + 1]
                    if nxt.isalpha():       # \macro …
                        j = i + 1
                        while j < n and buf[j].isalpha():
                            j += 1
                        if j >= n:          # macro name might continue next chunk; hold
                            break
                        if buf[j] == "{":   # \macro{ … } — enter brace-balanced macro
                            state = _MACRO; depth = 1; i = j + 1
                        else:               # bare macro (\to, \alpha) — plain text, safe
                            i = j; safe_end = i
                    else:                   # escape: \$  \\  \}  … — 2 safe chars
                        i += 2; safe_end = i
                else:
                    i += 1; safe_end = i
            elif state == _INLINE:
                if buf[i] == "$":
                    state = _TEXT; i += 1; safe_end = i
                elif buf[i] == "\n":        # invalid in inline math — flush, don't wedge
                    state = _TEXT; i += 1; safe_end = i
                else:
                    i += 1
            elif state == _DISPLAY:
                if buf[i:i + 2] == "$$":
                    state = _TEXT; i += 2; safe_end = i
                elif i + 1 >= n and buf[i] == "$":   # partial closing '$'; hold
                    break
                else:
                    i += 1
            elif state == _PAREN:
                if buf[i:i + 2] == "\\)":
                    state = _TEXT; i += 2; safe_end = i
                elif i + 1 >= n and buf[i] == "\\":  # partial closer; hold
                    break
                else:
                    i += 1
            elif state == _BRACKET:
                if buf[i:i + 2] == "\\]":
                    state = _TEXT; i += 2; safe_end = i
                elif i + 1 >= n and buf[i] == "\\":
                    break
                else:
                    i += 1
            elif state == _MACRO:
                if buf[i] == "{":
                    depth += 1; i += 1
                elif buf[i] == "}":
                    depth -= 1; i += 1
                    if depth == 0:
                        state = _TEXT; safe_end = i
                else:
                    i += 1
            elif state == _CODE:
                if buf[i:i + 3] == "```":
                    state = _TEXT; i += 3; safe_end = i
                else:
                    i += 1
        return safe_end


def latex_safe_stream(deltas: Iterable[str]) -> Iterator[str]:
    """Wrap a raw text-chunk stream, yielding only FOIM-safe (balanced) prefixes."""
    buf = LatexSafeBuffer()
    for d in deltas:
        out = buf.feed(d)
        if out:
            yield out
    tail = buf.flush()
    if tail:
        yield tail


# --- delimiter normalization ----------------------------------------------
# Open WebUI renders \(…\), \[…\], $$…$$ reliably but single $…$ mid-prose unreliably
# (issue #21612, "Intended"). Convert paired single-$ inline math to \(…\) so it renders
# without forking the frontend. $$…$$ display and \(…\)/\[…\] pass through untouched.
# Applied AFTER the FOIM buffer, so each `$…$` arrives complete (the buffer holds it until
# its closing '$'), which is exactly what this regex needs.
_INLINE_DOLLAR = re.compile(r"(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)")


def normalize_delimiters(text: str) -> str:
    """Rewrite single ``$…$`` inline math to ``\\(…\\)`` (leave ``$$``/``\\(``/``\\[`` alone)."""
    return _INLINE_DOLLAR.sub(r"\\(\1\\)", text)
