"""Fan-out (shared): maj@k for the solver, Pass@k for the prover.

A single chain is irreducibly sequential — each block depends on the last. The
parallelism is across independent *samples* and *problems*: maj@k draws, problem-set
entries, a prover's Pass@k chains. Wrap one full chain as a Modal function and `.map`
over seeds — each call gets its own executor/verifier; the vLLM endpoint batches their
generation requests via @modal.concurrent. Scale-to-zero means the burst costs only the
seconds it runs.

UNTESTED — illustrative of the pattern in the post's #fan-out. The solver path is the
spine; the prover path is the same shape with the verifier as the per-call executor.
"""
import os
from collections import Counter

import modal

app = modal.App("pudding-fanout")

# Each chain runs on Modal with its own in-container IPython kernel (so the k chains'
# kernel state can't collide), pulling tokens from the metered endpoint. The image carries
# the orchestrator source + the local Kernel's deps. The Featherless key is read from the
# local .env (same source of truth as direnv) and bundled as an app-scoped ephemeral
# secret — nothing persistent to create or manage. See README "Setup".
chain_img = (
    modal.Image.debian_slim()
    .pip_install("openai>=1.0", "jupyter_client", "ipykernel", "sympy", "numpy", "scipy")
    .add_local_python_source("solver_loop")
)
featherless_secret = modal.Secret.from_dotenv()  # reads FEATHERLESS_API_KEY from .env

# Fan-out width is bounded by the *endpoint's* concurrency, not by Modal — a wider .map
# gets 429s. This is a provider/plan PARAMETER, not a constant: Featherless feather_pro_plus
# = 4, its higher tier = 8, Novita/others differ, and self-hosting serve.py (vLLM +
# @modal.concurrent) lifts it entirely. Set ENDPOINT_CONCURRENCY to your plan's limit.
#
# Aside: this fans out k SEPARATE requests (one full prefill each). The efficient alternative
# is server-side parallel sampling — n=k in ONE request, prompt prefill shared and the k
# chains decoded together. Self-hosted vLLM (serve.py) supports it efficiently; Novita exposes
# n (<=128, per-token); Featherless's chat API and OpenRouter do NOT — which is why we .map
# here. Full provider table + recommendation in PLAN.md §10.
ENDPOINT_CONCURRENCY = int(os.environ.get("ENDPOINT_CONCURRENCY", "4"))


# --- SOLVER: maj@k ---------------------------------------------------------
@app.function(image=chain_img, secrets=[featherless_secret], timeout=600,
              max_containers=ENDPOINT_CONCURRENCY)
def solve_one(problem: str, seed: int) -> str | None:
    """One full TIR chain, its own in-container Kernel. Returns the boxed answer.

    Seeded temperature ~0.6 so the k chains diverge (single greedy decode would make
    maj@k pointless). Each .map call is its own container → its own kernel → no state
    bleed between chains.
    """
    from solver_loop import solve, Kernel, extract_boxed

    transcript = solve(problem, executor=Kernel(), temperature=0.6, seed=seed)
    return extract_boxed(transcript)


def majority_vote(problem: str, k: int = 32):
    """maj@k: k independent chains, vote their boxed answers. The post's #fan-out."""
    answers = list(solve_one.map([problem] * k, range(k)))
    votes = Counter(a for a in answers if a)
    return votes.most_common(1)[0][0] if votes else None


@app.local_entrypoint()
def main(problem: str = "Find the remainder when 7^999 is divided by 1000.", k: int = 8):
    print(f"maj@{k}: {majority_vote(problem, k)}")


# --- PROVER: Pass@k --------------------------------------------------------
# The same shape: map a `prove_one(statement, seed)` (one prover_loop.prove chain, its own
# LeanVerifier) over seeds, and collect the FIRST closing proof instead of voting:
#
#   proofs = prove_one.map([statement] * k, range(k))
#   first_ok = next((p for p in proofs if p["ok"]), None)
