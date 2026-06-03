"""Orchestrator (PRIMARY): the TIR solver loop — remote tokens, local executor.

This is the spine of the repo. The model writes Python in ```python fences; we run each
block in a stateful IPython kernel and splice the result back inside a ```output fence;
repeat until the model emits no fresh code. The prover (prover_loop.py) is the SAME shape
with a Lean verifier in place of the Python kernel and a different halting rule.

`executor` is anything with `.run(code) -> str`: the local `Kernel` below by default, or
the remote `executor/modal_executor.py` Sandbox when the computation is heavy.

UNTESTED. See PLAN.md §5 for the testing order (the solver is steps 1–5).
"""
import os

from openai import OpenAI

MODEL = "Qwen/Qwen2.5-Math-7B-Instruct"

# Generous generation budget by default. A TIR answer carries reasoning + code + the final
# \boxed{}; a stingy cap truncates multi-step solutions before the answer ever appears
# (1024/round dropped ~20% of GSM8K to empty answers). These are per-round / per-loop maxima.
MAX_TOKENS = 8192
MAX_CALLS = 8

SYS = ("Please integrate natural language reasoning with programs to solve the "
       "problem above, and put your final answer within \\boxed{}.")

# Qwen2.5-Math writes a ```python block and expects the result in a ```output block.
# (OpenMath-Nemotron uses <tool_call>…</tool_call> instead — read the tags off the
# model's docs, not its name; parametrize here if you swap models. PLAN.md §2.1.)
TICK = chr(96) * 3
CODE_OPEN, OUT_OPEN, CLOSE = TICK + "python", TICK + "output", TICK


class Kernel:
    """Local stateful IPython kernel: variables persist across code blocks, so a value
    defined in the first block is visible in the third. A fresh subprocess per block
    would break multi-step solutions."""

    def __init__(self):
        from jupyter_client import KernelManager
        self.km = KernelManager()
        self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()

    def run(self, code, timeout=10):
        self.kc.execute(code)
        chunks = []
        while True:
            try:
                m = self.kc.get_iopub_msg(timeout=timeout)
            except Exception:
                chunks.append("[timeout]")
                break
            t, c = m["msg_type"], m["content"]
            if t == "stream":
                chunks.append(c["text"])
            elif t in ("execute_result", "display_data"):
                chunks.append(c["data"].get("text/plain", ""))
            elif t == "error":
                chunks.append("\n".join(c["traceback"]))
            elif t == "status" and c["execution_state"] == "idle":
                break
        return "".join(chunks)[:1000]  # cap so a runaway print can't flood context


def make_client() -> OpenAI:
    # Chosen path is a metered endpoint (Featherless), executor stays local. Point
    # SOLVER_BASE_URL at a self-hosted Modal serve.py URL (or any vLLM endpoint) to swap.
    return OpenAI(
        base_url=os.environ.get("SOLVER_BASE_URL", "https://api.featherless.ai/v1"),
        api_key=os.environ.get("FEATHERLESS_API_KEY")
        or os.environ.get("SOLVER_API_KEY")
        or os.environ.get("VLLM_KEY", "EMPTY"),
    )


def last_code(text: str):
    return text.rsplit(CODE_OPEN, 1)[1].split(CLOSE, 1)[0] if CODE_OPEN in text else None


def extract_boxed(text: str) -> str | None:
    """Content of the LAST \\boxed{...}, brace-balanced so nested braces survive.

    The solver's answer; maj@k (fanout.py) votes over these across k chains.
    """
    start = text.rfind("\\boxed{")
    if start == -1:
        return None
    i, depth, out = start + len("\\boxed{"), 1, []
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
        i += 1
    return "".join(out).strip() if depth == 0 else None


def solve(problem: str, executor=None, max_calls: int = MAX_CALLS, client: OpenAI | None = None,
          temperature: float = 0.0, seed: int | None = None, model: str | None = None,
          max_tokens: int = MAX_TOKENS) -> str:
    """Generate → run code → feed result back, until the model emits no fresh block.

    `executor` has `.run(code) -> str` (default: a local `Kernel`). For the remote
    executor, pass `modal.Cls.from_name("pudding-executor", "Executor")()` and note that
    Modal calls it as `executor.run.remote(code)` — the one-line adapter below handles
    both. Returns the full transcript; the answer is the last \\boxed{...} (extract_boxed).

    `temperature`>0 + a distinct `seed` per call is what makes the k chains of maj@k
    diverge (fanout.py); the single greedy default is for interactive one-shots.
    """
    client = client or make_client()
    model = model or MODEL
    executor = executor or Kernel()
    run = getattr(executor.run, "remote", executor.run)  # Modal class vs local object

    prompt = (f"<|im_start|>system\n{SYS}<|im_end|>\n"
              f"<|im_start|>user\n{problem}<|im_end|>\n<|im_start|>assistant\n")
    for _ in range(max_calls + 1):
        r = client.completions.create(
            model=model, prompt=prompt, temperature=temperature, max_tokens=max_tokens,
            seed=seed,
            stop=[OUT_OPEN, "\n\n---"],   # halt the moment a tool result is wanted
        )
        prompt += r.choices[0].text
        code = last_code(r.choices[0].text)
        if code is None or not code.strip():  # no fresh code (or a degenerate empty ``` fence) -> done
            break
        prompt += f"{OUT_OPEN}\n{run(code)}\n{CLOSE}\n"
    return prompt


if __name__ == "__main__":
    # Smoke test against a local kernel (needs jupyter_client + ipykernel installed).
    import sys
    problem = " ".join(sys.argv[1:]) or "Find the remainder when 7^999 is divided by 1000."
    transcript = solve(problem)
    print(transcript)
    print("=" * 60)
    print(f"boxed answer: {extract_boxed(transcript)}")
