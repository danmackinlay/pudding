# Prover research addendum (for stage 2)

Captured 2026-06-03 while scoping `PLAN.md`. We then realised the build order is
backwards — the **solver** loop should come first, the prover is stage 2. This file
freezes the prover research (verified against primary sources / real code) so we don't
re-derive it on return. It **corrects two guesses in `PLAN.md`** (marked ⚠️).

## TL;DR — what changed vs PLAN.md

- ⚠️ **No multi-hour `lean_image` build is needed.** A prebuilt public image exists:
  `projectnumina/kimina-lean-server:2.0.0` (Mathlib **v4.26.0** already baked in). The
  entire §3 "crux" build risk collapses to `modal.Image.from_registry(...)`. Build from
  the Dockerfile only if we need a *different* Lean version than 4.26.0 (see version-match risk).
- ⚠️ **The Kimina API is `POST /api/check`**, not `/verify`. Payload uses `snippets` +
  `id` + `code`, not `codes` + `custom_id`. (PLAN §2.1 guessed the older/0.x shape.)
- The metered **DeepSeek-Prover-V2-671B route is live on Novita** (~$0.70/$2.50 per 1M,
  OpenAI-compatible at `https://api.novita.ai/openai`, 160K ctx) — confirmed against the
  catalog (https://novita.ai/models/model-detail/deepseek-deepseek-prover-v2-671b). This is
  the blog's "cheapest entry = meter the 671B" route; needs a Novita API key.

## 1. Verifier image — the proven recipe (from the Modal case study)

Source code (cloned & read, not just the blog): https://github.com/agencyenterprise/modal-rl-theorem-case-study
— `experiments/modal_app.py`, `experiments/lean_utils.py`.

```python
lean_server_image = modal.Image.from_registry("projectnumina/kimina-lean-server:2.0.0")
```

They run it in a **`modal.Sandbox`** (not `@app.cls`), per-batch, because a runaway proof
can wedge the Lean server — Sandbox isolation + a restart-on-timeout path is their answer.
PLAN §3 says "start `@app.cls`, move to Sandbox if it wedges"; the case study already
learned that lesson and went straight to Sandbox. Their loop:

1. `sb = modal.Sandbox.create(image=lean_server_image, cpu=1, memory=8192, timeout=300, encrypted_ports=[8000])`
2. `sb.exec("bash","-c","cd /root/kimina-lean-server && nohup python -m server > /tmp/server.log 2>&1 &")`
3. `server_url = sb.tunnels()[8000].url`
4. Poll `GET {server_url}/health` until 200 (≤180 s).
5. For each proof: `POST {server_url}/api/check` (below).
6. On a proof exceeding `timeout_per_proof`: `pkill -9 -f 'python -m server'` and restart the server.
7. `finally: sb.terminate()`.

For our pedagogical single-proof loop, an `@app.cls` holding a warm server on
`localhost:8000` is still the simplest first cut (start the server in `@modal.enter()`,
POST to `http://localhost:8000/api/check`). Keep the Sandbox restart trick in mind for fan-out.

### The actual /api/check contract (kimina-lean-server 2.0.0)

Request:
```python
requests.post(f"{server_url}/api/check", json={
    "snippets": [{"id": "proof", "code": full_code}],
    "timeout": timeout_per_proof,     # seconds, per proof
}, timeout=timeout_per_proof + 30)
```

Response parsing (a closed proof = no error-severity message):
```python
resp = response.json()["results"][0]
messages = resp.get("response", {}).get("messages", [])
errors = [m for m in messages if m.get("severity") == "error"]
complete = len(errors) == 0          # also assert no `sorries` to be safe (PLAN §2.1)
```

`full_code = f"{header}\n{statement}\n{proof}"`. The case study's header:
```
import Mathlib
import Aesop
set_option maxHeartbeats 0
open BigOperators Real Nat Topology Rat
```

## 2. Version pins (the brittle part)

From the real Kimina `Dockerfile` + `setup.sh` (cloned from project-numina/kimina-lean-server):

| Knob | Value in 2.0.0 |
|---|---|
| `LEAN_SERVER_LEAN_VERSION` (default) | `v4.26.0` |
| repl | `https://github.com/FrederickPu/repl.git` branch `lean415compat` (a fork, not leanprover-community/repl) |
| mathlib4 branch | `${LEAN_SERVER_LEAN_VERSION}` → `v4.26.0` |
| cache | `setup.sh` runs `lake exe cache get` then `lake build` (so the multi-hour rebuild is avoided *inside their build*, and it's already baked into the published image) |
| base | `python:3.13-slim`, `CMD ["python","-m","server"]`, server on `:8000` |

Build a different version with: `docker build --build-arg=LEAN_SERVER_LEAN_VERSION=v4.21.0 .`
Supported range observed in README/Dockerfile: v4.9.0 … v4.26.0 (a `<=v4.9.0` cherry-pick path exists in setup.sh).

### ⚠️ Version-match risk (verify before blaming the prover)
- **DeepSeek-Prover-V2** (≈ April 2025, arXiv 2504.21801): model card / GitHub do **not**
  state a `leanprover/lean4:vX` pin explicitly; proofs just `import Mathlib`. Released when
  Mathlib was ≈ v4.18–4.20, i.e. **older than the verifier's 4.26.0**. Renamed/moved lemmas
  could make valid proofs fail to compile. Confirm the model's target Mathlib before trusting failures.
- **Goedel-Prover-V2** GitHub README cites "Lean 4 version 4.9 and the corresponding Mathlib",
  submodule `mathlib4 @ 2f65ba7`. This reads like a **stale/V1 carry-over** (4.9 is very old
  for an Aug-2025 model) — treat as unconfirmed; re-check the V2 card directly.
- Mitigation if spurious failures appear: rebuild the Kimina image with a `LEAN_SERVER_LEAN_VERSION`
  matching the chosen model, instead of using the 4.26.0 prebuilt. That reintroduces the build cost.

## 3. Model serving (I/O contract confirmed)

- **Prompt template** (DeepSeek-Prover-V2 *and* Goedel-Prover-V2, verbatim from the HF card —
  matches PLAN §2.2 and `prover_loop.py`):
  ```
  Complete the following Lean 4 code:

  ```lean4
  {statement}
  ```

  Before producing the Lean 4 code to formally prove the given theorem, provide a detailed
  proof plan outlining the main proof steps and strategies.
  The plan should highlight key ideas, intermediate lemmas, and proof structures that will
  guide the construction of the final formal proof.
  ```
  Single user turn via `apply_chat_template(..., add_generation_prompt=True)`.
- **Budgets:** 7B context 32K; card example uses `max_new_tokens=8192`. Goedel-32B up to 32K,
  ~40K with self-correction (PLAN §2.3).
- **Extraction:** last ```` ```lean4 … ``` ```` fence, regex `r'```lean4\n(.*?)\n```'` DOTALL — as coded.

### Serving options for stage 2 (the open decision)
1. **Self-host on Modal GPU** — `deepseek-ai/DeepSeek-Prover-V2-7B` on H100, scale-to-zero,
   vLLM OpenAI-compatible. No external signup (Modal already authed on this machine). DeepSeek-Prover-V2-7B
   appears ungated (confirm). This is the most faithful to PLAN and deployable immediately.
2. **Local MLX on the Mac** — blog suggests DeepSeek-Prover-V2-7B → Goedel-Prover-V2-32B (MLX 8-bit).
   Zero cloud cost, fully private, slower. Good laptop-dev path.
3. **Metered hosted endpoint** — DeepSeek-Prover-V2-671B on **Novita** (OpenAI-compatible,
   `https://api.novita.ai/openai`, ~$0.70/$2.50 per 1M). Needs a Novita API key from the user.
- The case study's own default model is the tiny `AI-MO/Kimina-Prover-Preview-Distill-1.5B` —
  a cheap smoke-test model if we just want the loop wired before committing GPU.

## 4. Local toolchain gotcha
- This machine's default `python` is **3.14** (pyenv shim). Modal client 1.1.4 may not support 3.14 —
  use a pinned venv (e.g. `uv venv --python 3.12`) for `modal deploy`/run. (Hit before I could verify; flagged.)

## 5. What's already scaffolded in this repo (stage-2 starting point)
- `serve.py` — Modal GPU vLLM server, `MODEL = deepseek-ai/DeepSeek-Prover-V2-7B`. Sound; just untested.
- `prover_loop.py` — prompt build, fence extraction, compile-and-retry. Sound. ⚠️ its stub verifier
  must be replaced and the verifier `.run()` mapped to the `/api/check` contract above (not `/verify`).
- `verifier/modal_verifier.py` — placeholder `lean_image` that "WILL NOT WORK as written." Replace with
  `modal.Image.from_registry("projectnumina/kimina-lean-server:2.0.0")` and wire `run()` to `/api/check`.
- `verifier/lean_image_notes.md` — the DIY build recipe; now mostly moot given the prebuilt image, but
  keep it for the version-match-rebuild fallback.

## Sources
- Kimina Lean Server (Dockerfile/setup.sh read directly): https://github.com/project-numina/kimina-lean-server · paper https://arxiv.org/abs/2504.21230
- Modal RL theorem case study (code read directly): https://github.com/agencyenterprise/modal-rl-theorem-case-study · blog https://modal.com/blog/building-an-rl-theorem-proving-workflow-on-modal
- DeepSeek-Prover-V2: https://github.com/deepseek-ai/DeepSeek-Prover-V2 · https://huggingface.co/deepseek-ai/DeepSeek-Prover-V2-7B · arXiv 2504.21801
- Goedel-Prover-V2: https://github.com/Goedel-LM/Goedel-Prover-V2 · arXiv 2508.03613
- Novita DeepSeek-Prover-V2-671B (serverless, confirmed 2026-06): https://novita.ai/models/model-detail/deepseek-deepseek-prover-v2-671b
