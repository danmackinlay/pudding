"""Draft-sketch-prove (Track 2 — the glass-box spine). PROVER_PLAN §1½/§4½.

The transparency win: the **informal sketch is the plan**, and the compiler validates the
skeleton's *logic* BEFORE we spend on leaves. A generalist drafts a flat decomposition —
self-contained `lemma hᵢ : Tᵢ := by sorry` leaves + a `theorem target := by <combine the hᵢ>` —
Kimina confirms the combine type-checks (no errors; the lemma `sorry`s are the only holes), then
each leaf is closed by the Q1 leaf-prover (`prove.prove_one_async`) in parallel, the proofs are
spliced back, and the whole file is re-verified end-to-end.

A *flat* decomposition (lemmas + one combine) — not yet recursive; recursion on un-closable leaves
is the "thin recursion" depth knob (PROVER_PLAN §1½), deliberately deferred. Reuses the deployed
Kimina verifier + Q1; no shared-core change.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from providers import make_async_client

from .prove import (DEFAULT_MAX_TOKENS, DEFAULT_PROVER_MODEL, _check, _get_verifier, _one_call,
                    extract_proof, prove_one_async)

DEFAULT_SKETCH_ROUNDS = 2     # self-corrections on the SKELETON's combine step (errors fed back) — cheap

SKETCH_SYS = ("You are an expert in Lean 4 and Mathlib. You decompose a theorem into a few "
              "self-contained helper lemmas plus a short proof that combines them.")

SKETCH_USER = """Decompose this Lean 4 theorem into a proof SKETCH.

```lean4
{target}
```

Output ONE ```lean4 code block containing, in order:
1. `import Mathlib`
2. each helper lemma as `lemma <name> : <statement> := by sorry`  (leave the body `sorry` — we \
close them separately)
3. the theorem, its signature EXACTLY as given, with a COMPLETE proof that combines the lemmas.

Rules:
- Each helper lemma MUST be self-contained: universally-quantify every variable it needs, so it \
proves on its own. Do NOT reference the theorem's local hypotheses inside a lemma.
- Keep each lemma signature on a SINGLE line, no trailing comment.
- 2–4 helper lemmas that carry the real work; the combine step must compile (only the lemma \
bodies stay `sorry`).
Give a one-line plan, then the code block."""

SKETCH_RETRY = (
    "The Lean compiler REJECTED the SKELETON — the combine step (the theorem's proof that ties the "
    "lemmas together) does not type-check:\n\n{errors}\n\nFix it so the skeleton elaborates with the "
    "helper lemmas still left as `sorry`. You may adjust the helper-lemma statements, but keep each "
    "self-contained and keep the theorem signature unchanged. Output the full ```lean4 block again."
)

# A `lemma`/`theorem` line whose body is left as `sorry` (the holes). One per line (we instruct so).
_DECL_SORRY = re.compile(r"^\s*(lemma|theorem)\s+(\w+)\b.*?:=\s*(?:by\s+)?sorry\s*$")


@dataclass
class Leaf:
    name: str
    line: str                       # the full `lemma <name> … := by sorry` line from the skeleton
    ok: bool = False
    proof: str | None = None        # the proved lemma block (import-stripped) when closed
    error: str | None = None
    rounds: int = 0


@dataclass
class Sketch:
    target: str
    raw: str = ""                   # the model's full reply (plan + fence)
    skeleton: str = ""              # the extracted lean4 file (lemmas-as-sorry + theorem-combine)
    skeleton_ok: bool = False       # combine type-checks: no errors, theorem not a bare sorry, ≥1 hole
    skeleton_errors: list = field(default_factory=list)
    sketch_rounds: int = 0          # self-corrections spent getting the combine to type-check
    leaves: list = field(default_factory=list)      # list[Leaf]
    reassembled: str = ""
    final_ok: bool = False          # the reassembled whole proof compiles (no errors, no sorries)
    final_errors: list = field(default_factory=list)
    error: str | None = None        # engine/transport error
    markdown: str = ""
    elapsed_s: float = 0.0


# --- parsing ---------------------------------------------------------------
def parse_leaves(skeleton: str) -> list:
    """The helper `lemma … := by sorry` lines are the holes; the `theorem` combines, so it's not one."""
    out = []
    for line in skeleton.splitlines():
        m = _DECL_SORRY.match(line)
        if m and m.group(1) == "lemma":
            out.append(Leaf(name=m.group(2), line=line.rstrip()))
    return out


def _theorem_is_sorry(skeleton: str) -> bool:
    return any(re.match(r"^\s*theorem\b.*?:=\s*(?:by\s+)?sorry\s*$", ln) for ln in skeleton.splitlines())


def _strip_imports(code: str) -> str:
    keep = [ln for ln in (code or "").splitlines() if not ln.strip().startswith("import ")]
    return "\n".join(keep).strip()


def _err1(errors: list) -> str:
    return str((errors or [{}])[0].get("data") or (errors or [""])[0])[:160] if errors else ""


# --- the steps -------------------------------------------------------------
def _initial_msgs(target: str) -> list:
    return [{"role": "system", "content": SKETCH_SYS},
            {"role": "user", "content": SKETCH_USER.format(target=target)}]


async def _draft(client, model, msgs, temperature, seed, max_tokens):
    """One sketch generation against the running conversation → (reply_text, skeleton|None)."""
    text, _reasoning, _info = await _one_call(client, model, msgs, temperature, seed, max_tokens)
    skeleton = extract_proof(text)            # last ```lean4 fence
    if skeleton and "import " not in skeleton:
        skeleton = "import Mathlib\n\n" + skeleton
    return text, skeleton


async def draft_sketch(target: str, *, model, temperature, seed, max_tokens, client):
    """Convenience single draft (no self-correction) — for tests / exploration."""
    return await _draft(client, model, _initial_msgs(target), temperature, seed, max_tokens)


async def check_skeleton(skeleton: str, *, verifier, timeout: int) -> tuple[bool, list]:
    """Skeleton-validity: the combine must type-check. The lemma `sorry`s are EXPECTED, so we look
    at *errors only* — plus we require the theorem itself isn't punted to `sorry`."""
    verdict = await _check(verifier, skeleton, timeout, add_header=False)
    errors = verdict.get("errors") or []
    return (not errors), errors


async def close_leaf(leaf, *, verifier, client, model, leaf_k, max_tokens, verify_timeout):
    """Close one leaf-lemma with the Q1 leaf-prover (Pass@k per leaf — first that compiles wins)."""
    leaf_code = f"import Mathlib\n\n{leaf.line}"
    best = None
    for i in range(leaf_k):
        r = await prove_one_async(leaf_code, model=model, temperature=(0.0 if leaf_k == 1 else 0.6),
                                  seed=i, max_tokens=max_tokens, verifier=verifier, client=client,
                                  verify_timeout=verify_timeout)
        best = r
        if r["ok"]:
            break
    leaf.ok = bool(best["ok"])
    leaf.rounds = best["rounds"]
    leaf.proof = _strip_imports(best["proof"]) if best["ok"] else None
    leaf.error = None if best["ok"] else (best["error"] or _err1(best["lean_errors"]) or "open")
    return leaf


def reassemble(skeleton: str, leaves: list) -> str:
    """Splice each closed leaf's proof back in place of its `:= by sorry` line."""
    out = skeleton
    for leaf in leaves:
        if leaf.ok and leaf.proof:
            out = out.replace(leaf.line, leaf.proof)
    return out


# --- the public verb -------------------------------------------------------
async def prove_by_sketch(target: str, *, model: str | None = None, provider: str | None = "openrouter",
                          temperature: float = 0.2, seed: int = 0, max_tokens: int = DEFAULT_MAX_TOKENS,
                          leaf_k: int = 1, sketch_rounds: int = DEFAULT_SKETCH_ROUNDS,
                          verify_timeout: int = 120, verifier=None, client=None) -> Sketch:
    """Draft-sketch-prove a (formal) `theorem … := by sorry` target → a `Sketch` with the
    human-readable plan, per-leaf status, and an end-to-end-verified whole proof (or an honest
    partial). Reuses the deployed Kimina verifier + the Q1 leaf-prover."""
    model = model or DEFAULT_PROVER_MODEL
    verifier = verifier or _get_verifier()
    client = client or make_async_client(provider)
    t0 = time.perf_counter()
    s = Sketch(target=target)
    try:
        # 1+2. draft → validate the PLAN → self-correct the combine. The glass-box win: the compiler
        # rejects a broken plan (cheap) before we spend anything proving leaves.
        msgs = _initial_msgs(target)
        for round_i in range(sketch_rounds + 1):
            s.sketch_rounds = round_i
            s.raw, s.skeleton = await _draft(client, model, msgs, temperature, seed, max_tokens)
            if not s.skeleton:
                s.error = "model produced no ```lean4 skeleton"
                return _finish(s, t0)
            no_errors, s.skeleton_errors = await check_skeleton(s.skeleton, verifier=verifier,
                                                                timeout=verify_timeout)
            s.leaves = parse_leaves(s.skeleton)
            s.skeleton_ok = no_errors and bool(s.leaves) and not _theorem_is_sorry(s.skeleton)
            if s.skeleton_ok or round_i == sketch_rounds:
                break
            errtext = "\n".join(str(e.get("data") or e) for e in (s.skeleton_errors or [])[:6]) \
                      or "the theorem was left `sorry`, or there were no helper lemmas"
            msgs += [{"role": "assistant", "content": s.raw},        # feed the combine error back
                     {"role": "user", "content": SKETCH_RETRY.format(errors=errtext)}]
        if not s.skeleton_ok:
            return _finish(s, t0)                  # still broken after retries — honest stop

        # 3. close every leaf in parallel (each via the Q1 leaf-prover)
        await asyncio.gather(*(close_leaf(lf, verifier=verifier, client=client, model=model,
                                          leaf_k=leaf_k, max_tokens=max_tokens,
                                          verify_timeout=verify_timeout) for lf in s.leaves))

        # 4. reassemble + final verify (the real verdict)
        s.reassembled = reassemble(s.skeleton, s.leaves)
        if all(lf.ok for lf in s.leaves):
            fv = await _check(verifier, s.reassembled, verify_timeout, add_header=False)
            s.final_ok, s.final_errors = bool(fv.get("ok")), fv.get("errors") or []
    except Exception as e:        # noqa: BLE001 — surface, never crash the caller
        s.error = f"{type(e).__name__}: {e}"
    return _finish(s, t0)


def _finish(s: Sketch, t0: float) -> Sketch:
    s.elapsed_s = round(time.perf_counter() - t0, 1)
    s.markdown = render_sketch(s)
    return s


# --- the glass-box render --------------------------------------------------
def render_sketch(s: Sketch) -> str:
    head = (s.target.splitlines() or [""])[-1][:80]
    L = [f"### draft-sketch-prove · `{head}`", ""]
    if s.error:
        return "\n".join(L + [f"**engine error:** {s.error}"])
    if not s.skeleton:
        return "\n".join(L + ["**no skeleton produced.**"])

    valid = "✓ valid plan" if s.skeleton_ok else "✗ INVALID plan"
    L += [f"**skeleton:** {valid} · {len(s.leaves)} leaf-lemma(s) · {s.sketch_rounds} self-correction(s)",
          "", "```lean4", s.skeleton, "```", ""]
    if not s.skeleton_ok:
        why = ("the combine step does not type-check" if s.skeleton_errors
               else "no helper lemmas, or the theorem itself was left `sorry`")
        L += [f"> plan rejected before spending on leaves — {why}."]
        if s.skeleton_errors:
            L += [f"> `{_err1(s.skeleton_errors)}`"]
        return "\n".join(L)

    n_ok = sum(1 for lf in s.leaves if lf.ok)
    L += [f"**leaves:** {n_ok}/{len(s.leaves)} closed by the leaf-prover", ""]
    for lf in s.leaves:
        L += [f"- {'✓' if lf.ok else '✗'} `{lf.name}` "
              + (f"(round {lf.rounds})" if lf.ok else f"— {lf.error or 'open'}")]
    L += [""]

    if s.final_ok:
        L += ["**whole proof VERIFIED** ✓ — reassembled and compiler-checked end-to-end.", "",
              "```lean4", s.reassembled, "```"]
    else:
        L += [f"**incomplete:** {n_ok}/{len(s.leaves)} leaves closed; the whole proof is not yet "
              f"verified — an honest partial, not a laundered ✓."]
        if s.final_errors:
            L += [f"> `{_err1(s.final_errors)}`"]
    return "\n".join(L)
