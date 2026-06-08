"""Lean verifier on Modal — the prover's unfakeable executor.

Same `run(code) -> {ok, errors, ...}` shape as the solver's IPython sandbox, Lean
underneath. Wraps the **prebuilt** Kimina Lean Server image (Mathlib v4.26.0 already baked
in — PROVER_RESEARCH_ADDENDUM §1), so there is no multi-hour `lean_image` build: the §3
"crux" collapses to `from_registry(...)`.

Usage:
  smoke test (ephemeral):   modal run verifier/modal_verifier.py
  deploy for the engine:    modal deploy verifier/modal_verifier.py
                            → pudding/prove.py looks it up with modal.Cls.from_name(...)

Design (per the addendum): a warm `@app.cls` holds the Kimina FastAPI server on
localhost:8000 (started in `@modal.enter()`); `run()` POSTs to `/api/check`. The Kimina
server already pools REPLs with an import-header cache, so one warm container serves k
concurrent Pass@k checks. If a runaway proof ever wedges the server, switch each proof to
its own `modal.Sandbox` with the restart-on-timeout pattern (addendum §1) — not needed for
the spike.
"""
import json
import subprocess
import time
import urllib.error
import urllib.request

import modal

app = modal.App("pudding-verifier")

# The proven recipe (addendum §1): Mathlib v4.26.0 baked in, server on :8000, CMD
# `python -m server` from /root/kimina-lean-server. Rebuild from the Kimina Dockerfile with
# --build-arg LEAN_SERVER_LEAN_VERSION=vX only if a prover model targets a different Mathlib
# (the version-match risk, addendum §2). We start the server ourselves in @modal.enter()
# (Modal runs its own entrypoint, not the image CMD).
lean_image = modal.Image.from_registry("projectnumina/kimina-lean-server:2.0.0")

KIMINA_DIR = "/root/kimina-lean-server"
SERVER_URL = "http://localhost:8000"

# The case study's import header — everything a MiniF2F-style proof reaches for. The Kimina
# server caches this import (the whole point of the header cache), so `import Mathlib` is only
# slow on the very first check after a cold start.
HEADER = ("import Mathlib\n"
          "import Aesop\n"
          "set_option maxHeartbeats 400000\n"
          "open BigOperators Real Nat Topology Rat\n")


def _parse_check(result: dict) -> dict:
    """Map one Kimina /api/check result → {ok, errors, sorries, messages}.

    A *closed* proof = no error-severity message AND no `sorry` (addendum §1: "also assert
    no sorries to be safe"). `sorry` shows up as a `sorries` list and/or a warning whose text
    mentions sorry — we treat either as not-closed so a `:= by sorry` can never read as ✓.
    """
    resp = (result or {}).get("response", {}) or {}
    messages = resp.get("messages", []) or []
    errors = [m for m in messages if m.get("severity") == "error"]

    def _text(m: dict) -> str:
        return str(m.get("data") or m.get("text") or "")

    sorries = list(resp.get("sorries", []) or [])
    sorries += [m for m in messages if "sorry" in _text(m).lower()]
    # A server-level error (bad request, REPL crash) also means not-verified.
    if result.get("error"):
        errors = errors + [{"severity": "error", "data": str(result["error"])}]
    return {"ok": not errors and not sorries, "errors": errors,
            "sorries": sorries, "messages": messages}


@app.cls(
    image=lean_image,
    cpu=4.0,
    memory=16384,            # Lean REPLs are memory-hungry; ~a few GB each (addendum §3)
    scaledown_window=300,    # stay warm 5 min so back-to-back checks reuse the loaded Mathlib
    timeout=20 * 60,
)
class LeanVerifier:
    @modal.enter()
    def start(self):
        """Launch the in-image Kimina Lean Server and block until /health is 200."""
        self._log = open("/tmp/kimina-server.log", "w")
        self._proc = subprocess.Popen(
            ["python", "-m", "server"], cwd=KIMINA_DIR,
            stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + 180        # Mathlib import-cache load can take tens of seconds
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"Kimina server exited early ({self._proc.returncode})\n"
                                   + _tail("/tmp/kimina-server.log"))
            try:
                with urllib.request.urlopen(f"{SERVER_URL}/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(2)
        raise RuntimeError("Kimina server did not become healthy in 180s\n"
                           + _tail("/tmp/kimina-server.log"))

    @modal.method()
    def run(self, code: str, timeout: int = 60, add_header: bool = False) -> dict:
        """Check one Lean snippet. Returns {ok, errors, sorries, messages}.

        `code` is checked verbatim; pass `add_header=True` to prepend the standard Mathlib
        header (convenience for bare `theorem … := by …` statements).
        """
        full = f"{HEADER}\n{code}" if add_header else code
        payload = json.dumps(
            {"snippets": [{"id": "proof", "code": full}], "timeout": timeout}).encode()
        req = urllib.request.Request(f"{SERVER_URL}/api/check", data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 30) as r:
                body = json.loads(r.read())
        except Exception as e:        # noqa: BLE001 — a transport/timeout failure = not verified
            return {"ok": False, "errors": [{"severity": "error", "data": f"{type(e).__name__}: {e}"}],
                    "sorries": [], "messages": []}
        results = body.get("results") or [{}]
        return _parse_check(results[0])


def _tail(path: str, n: int = 40) -> str:
    try:
        with open(path) as f:
            return "--- server log (tail) ---\n" + "".join(f.readlines()[-n:])
    except OSError:
        return "(no server log)"


# --- smoke test: de-risk the verifier infra (Q1 step 1, PLAN §8) ------------
GOOD = "theorem pudding_smoke : 1 + 1 = 2 := by norm_num"
BAD = "theorem pudding_smoke_bad : 1 + 1 = 3 := by norm_num"
SORRY = "theorem pudding_smoke_sorry : 2 + 2 = 4 := by sorry"


@app.local_entrypoint()
def main():
    """`modal run verifier/modal_verifier.py` — prove the unfakeable compiler works.

    Expect: GOOD ok=True, BAD ok=False (norm_num can't close 1+1=3), SORRY ok=False
    (a `sorry` must never read as verified — the whole faithfulness story depends on this).
    """
    v = LeanVerifier()
    for name, code in [("GOOD", GOOD), ("BAD", BAD), ("SORRY", SORRY)]:
        t0 = time.time()
        verdict = v.run.remote(code, add_header=True)
        dt = time.time() - t0
        n_err = len(verdict["errors"])
        print(f"[{name}] ok={verdict['ok']}  errors={n_err}  sorries={len(verdict['sorries'])}  ({dt:.1f}s)")
        if not verdict["ok"] and n_err:
            print("      first error:", str(verdict["errors"][0].get("data"))[:200])
    print("\nExpected: GOOD ok=True · BAD ok=False · SORRY ok=False")
