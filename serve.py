"""Shared model server on Modal — the 'model server' role for both pipelines.

deploy:  modal deploy serve.py

Defaults to the TIR *solver* (Qwen2.5-Math), which is the spine of the repo. The *prover*
is a one-line model swap (see the PROVER block). vLLM serves either one OpenAI-style, so
solver_loop.py and prover_loop.py both just point an OpenAI client at the deployed URL.

UNTESTED. Versions / GPU / max-model-len are starting guesses — see PLAN.md §2.1, §3.3.
"""
import modal

# --- SOLVER (default — build and test this first) --------------------------
# Cheapest bring-up is the 7B; scale to Qwen2.5-Math-72B-Instruct or
# OpenMath-Nemotron-32B for real runs.
MODEL = "Qwen/Qwen2.5-Math-7B-Instruct"
MAX_LEN = 8192      # a solver answer + a few code blocks fit easily
GPU = "L40S"        # 7B fits; use "H100" for the 72B

# --- PROVER (extension — only after the solver runs, PLAN.md §3) -----------
# Swap the three lines above for a Lean prover:
#   MODEL   = "deepseek-ai/DeepSeek-Prover-V2-7B"   # or "Goedel-LM/Goedel-Prover-V2-32B"
#   MAX_LEN = 32768                                 # proof + CoT plan; ~40k for self-correction
#   GPU     = "H100"
# ...or skip self-hosting entirely and point prover_loop.py at
# DeepSeek-Prover-V2-671B on DeepInfra/Novita (the cheapest entry point, per the post).

image = (
    modal.Image.debian_slim()
    .pip_install("vllm==0.13.0", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)
hf_cache = modal.Volume.from_name("hf-cache", create_if_missing=True)
app = modal.App("pudding")


@app.function(
    image=image,
    gpu=GPU,
    volumes={"/root/.cache/huggingface": hf_cache},
    secrets=[modal.Secret.from_name("vllm-key")],  # injects VLLM_KEY, shared with the client
    scaledown_window=120,                           # scale to zero after 2 min idle
    timeout=20 * 60,
)
@modal.concurrent(max_inputs=16)
@modal.web_server(port=8000, startup_timeout=20 * 60)
def serve():
    import os
    import subprocess

    subprocess.Popen(
        f"vllm serve {MODEL} --host 0.0.0.0 --port 8000 "
        f"--max-model-len {MAX_LEN} "
        f"--api-key {os.environ['VLLM_KEY']}",
        shell=True,
    )
