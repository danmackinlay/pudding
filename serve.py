"""Prover model server on Modal — a model-swap of the solver's serve_qwen_math.py.

deploy:  modal deploy serve.py

UNTESTED. Versions (vllm, GPU, max-model-len) are starting guesses — see PLAN.md §2.3.
The prover takes a formal Lean 4 statement ending `:= by sorry` and emits a proof plan
+ proof; we read the answer off the last ```lean4 fence (prover_loop.py does that).
"""
import modal

# DeepSeek-Prover-V2-7B is the cheapest to bring up first (PLAN.md testing step 1).
# Swap to "Goedel-LM/Goedel-Prover-V2-32B" for self-correction / higher Pass@k,
# or point the orchestrator at DeepSeek-Prover-V2-671B on DeepInfra instead of self-hosting.
MODEL = "deepseek-ai/DeepSeek-Prover-V2-7B"

image = (
    modal.Image.debian_slim()
    .pip_install("vllm==0.13.0", "huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)
hf_cache = modal.Volume.from_name("hf-cache", create_if_missing=True)
app = modal.App("pudding-prover")


@app.function(
    image=image,
    gpu="H100",                                    # 7B fits easily; bump for 32B
    volumes={"/root/.cache/huggingface": hf_cache},
    secrets=[modal.Secret.from_name("vllm-key")],  # injects VLLM_KEY, shared with the client
    scaledown_window=120,                          # scale to zero after 2 min idle
    timeout=20 * 60,
)
@modal.concurrent(max_inputs=16)
@modal.web_server(port=8000, startup_timeout=20 * 60)
def serve():
    import os
    import subprocess

    # NB max-model-len must cover the long proof + CoT plan (PLAN.md §2.3):
    # ~8k for the 7B, 32k for Goedel-32B, ~40k once self-correction is on.
    subprocess.Popen(
        f"vllm serve {MODEL} --host 0.0.0.0 --port 8000 "
        f"--max-model-len 32768 "
        f"--api-key {os.environ['VLLM_KEY']}",
        shell=True,
    )
