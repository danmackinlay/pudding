"""Lean verifier on Modal — same `run(code) -> result` interface as the solver's
IPython sandbox, Lean underneath.

deploy:  modal deploy verifier/modal_verifier.py

The verifier wraps the Kimina Lean Server (a FastAPI pool over leanprover-community/repl
with an import-header cache; PLAN.md §2.1). The HARD part is `lean_image` — a Lean
toolchain + a *prebuilt* Mathlib (`lake exe cache get`, toolchain pinned to Mathlib's
exact version). See verifier/lean_image_notes.md. This file is UNTESTED scaffolding.
"""
import modal

app = modal.App("pudding-verifier")

# ---------------------------------------------------------------------------
# TODO(lean_image): this is the crux. See verifier/lean_image_notes.md.
# Easiest path: build from the Kimina Dockerfile (it installs Lean, repl, mathlib4),
# then wrap as a Modal image. Placeholder below WILL NOT WORK as written — it does not
# install Lean/Mathlib and does not `lake exe cache get`.
# ---------------------------------------------------------------------------
lean_image = (
    modal.Image.from_registry("python:3.11-slim")  # TODO: replace with a real Lean+Mathlib image
    .pip_install("kimina-client", "fastapi", "uvicorn")
    # .run_commands(... elan, lake exe cache get, kimina-lean-server setup.sh ...)
    .env({"LEAN_SERVER_MAX_REPL_MEM": "8g"})        # per-REPL OOM is real (PLAN.md §3)
)


@app.cls(
    image=lean_image,
    cpu=8.0,
    memory=32768,
    scaledown_window=120,
    # Start as a warm @app.cls. If a runaway proof wedges the server, switch each
    # proof to its own modal.Sandbox (the Modal case-study choice, PLAN.md §3).
)
class LeanVerifier:
    @modal.enter()
    def start(self):
        # TODO: launch the in-image Kimina Lean Server (uvicorn) and hold a client.
        # from kimina_client import KiminaClient
        # self.client = KiminaClient(base_url="http://localhost:8000")
        raise NotImplementedError("start Kimina Lean Server in lean_image")

    @modal.method()
    def run(self, proof: str, timeout: int = 300) -> dict:
        """Check one Lean proof. Returns {"ok": bool, "errors": [...]}.

        A closed proof = empty `sorries` AND no error-severity message (PLAN.md §2.1).
        """
        # result = self.client.check([{"custom_id": "p", "proof": proof}], timeout=timeout)
        # r = result[0]
        # errors = [m for m in r.get("messages", []) if m.get("severity") == "error"]
        # ok = not errors and not r.get("sorries")
        # return {"ok": ok, "errors": errors}
        raise NotImplementedError("POST proof to Kimina server, map response to {ok, errors}")


# ---------------------------------------------------------------------------
# Fan-out (Pass@k): one chain per call, its own verifier, `.map` over seeds.
# Mirrors the solver's maj@k in the blog post's #fan-out.
# ---------------------------------------------------------------------------
@app.function(image=lean_image, timeout=20 * 60)
def prove_one(statement: str, seed: int) -> dict:
    from prover_loop import prove  # noqa: PLC0415
    verifier = LeanVerifier()
    return prove(statement, verifier)  # TODO: thread `seed` into sampling temperature
