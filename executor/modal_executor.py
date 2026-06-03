"""Remote executor (extension of solver_loop.Kernel) — when the computation is heavy.

A Gröbner basis, a large symbolic integral, a Monte Carlo estimate, a Sage/PARI call, or
another model invoked as a tool can each dwarf the reasoning that asked for it. The TIR
loop only wants an `.run(code) -> str` box, so move it off the laptop onto a CPU-sized
Modal container — sized to the *tool*, not the GPU model box. A cheap CPU sandbox beside
the GPU endpoint beats holding an H100 busy doing SymPy.

Same interface as solver_loop.Kernel — only the decorators that lift it onto Modal differ.
In solver_loop.solve, pass `modal.Cls.from_name("pudding-executor", "Executor")()`; the
loop calls it as `.run.remote(code)` (solver_loop handles the adapter). UNTESTED.
"""
import modal

app = modal.App("pudding-executor")

sandbox_img = modal.Image.debian_slim().pip_install(
    "jupyter_client", "ipykernel", "sympy", "numpy", "scipy"
)


@app.cls(image=sandbox_img, cpu=8.0, memory=32768, scaledown_window=120)  # or gpu="L4"
class Executor:
    @modal.enter()
    def start(self):
        from jupyter_client import KernelManager
        self.km = KernelManager()
        self.km.start_kernel()
        self.kc = self.km.client()
        self.kc.start_channels()

    @modal.method()
    def run(self, code, timeout=60):
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
        return "".join(chunks)[:2000]
