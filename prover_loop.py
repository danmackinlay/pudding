"""Orchestrator: the compile-and-retry loop.

The solver's TIR loop stops when the model emits no fresh code. The prover's loop
stops when the *compiler* accepts the proof, and on failure feeds the compiler errors
back in place of a tool result (this IS Goedel-Prover-V2's self-correction). The only
new role vs the solver is the verifier — a `run(proof) -> {ok, errors}` box that runs
Lean instead of Python (see verifier/modal_verifier.py).

UNTESTED end-to-end. See PLAN.md §5 for the testing order.
"""
import os
import re
from openai import OpenAI

MODEL = "deepseek-ai/DeepSeek-Prover-V2-7B"

# DeepSeek-Prover-V2 and Goedel-Prover-V2 share this template verbatim (PLAN.md §2.2).
PROMPT_TEMPLATE = """Complete the following Lean 4 code:

```lean4
{statement}
```

Before producing the Lean 4 code to formally prove the given theorem, provide a \
detailed proof plan outlining the main proof steps and strategies."""

# Take the LAST ```lean4 fence (Goedel's extract regex).
_FENCE = re.compile(r"```lean4\n(.*?)\n```", re.DOTALL)


def extract_proof(text: str) -> str | None:
    matches = _FENCE.findall(text)
    return matches[-1] if matches else None


def make_client() -> OpenAI:
    # Point base_url at the Modal serve.py URL, or a metered endpoint (DeepInfra/Novita)
    # for the 671B — the orchestrator doesn't care where the tokens come from.
    return OpenAI(
        base_url=os.environ.get("PROVER_BASE_URL", "http://localhost:8000/v1"),
        api_key=os.environ.get("VLLM_KEY", "EMPTY"),
    )


def prove(statement: str, verifier, max_rounds: int = 2, client: OpenAI | None = None) -> dict:
    """Generate → verify → feed errors back, up to `max_rounds` self-corrections.

    `verifier` is anything with `.run(proof) -> {"ok": bool, "errors": list}` — a local
    Kimina client, or the Modal LeanVerifier (`verifier.run.remote(proof)`).
    Returns {"ok", "proof", "rounds", "errors"}.
    """
    client = client or make_client()
    messages = [{"role": "user", "content": PROMPT_TEMPLATE.format(statement=statement)}]
    last_errors: list = []

    for round_i in range(max_rounds + 1):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.0,        # greedy for a single attempt; raise for Pass@k diversity
            max_tokens=32768,       # proof + CoT plan run long (PLAN.md §2.3)
        )
        text = resp.choices[0].message.content
        proof = extract_proof(text)
        if proof is None:
            return {"ok": False, "proof": None, "rounds": round_i, "errors": ["no lean4 fence"]}

        verdict = verifier.run(proof)            # {"ok": bool, "errors": [...]}
        if verdict["ok"]:
            return {"ok": True, "proof": proof, "rounds": round_i, "errors": []}

        # self-correction: feed the compiler errors back as the next turn
        last_errors = verdict["errors"]
        messages.append({"role": "assistant", "content": text})
        messages.append({
            "role": "user",
            "content": "The Lean compiler reported errors:\n\n"
                       + "\n".join(str(e) for e in last_errors)
                       + "\n\nFix the proof and output the complete corrected Lean 4 code.",
        })

    return {"ok": False, "proof": proof, "rounds": max_rounds, "errors": last_errors}


if __name__ == "__main__":
    # Smoke test against a trivial statement. Swap in a real verifier client.
    SAMPLE = "import Mathlib\nset_option maxHeartbeats 400000\n\ntheorem pudding : 1 + 1 = 2 := by sorry"

    class _StubVerifier:
        """Replace with the Kimina client or Modal LeanVerifier (PLAN.md §2.1)."""
        def run(self, proof: str) -> dict:
            raise NotImplementedError("wire up verifier/modal_verifier.py")

    print(prove(SAMPLE, _StubVerifier()))
