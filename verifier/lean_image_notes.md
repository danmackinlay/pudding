# Building `lean_image` — the crux

This is the version-sensitive, multi-gigabyte, easy-to-get-wrong part. Everything else
in the repo is a model swap or a thin loop; this is why the repo exists. Expanded from
PLAN.md §3.

## The trap

Building Mathlib from source = 2500+ files, **hours**. You must NOT do that. The
prebuilt `.olean` cache (`lake exe cache get`) only matches if your `lean-toolchain`
is **byte-identical** to the version Mathlib's CI built against. Any mismatch silently
falls back to a full source rebuild — the build "works" but takes hours and you assume
something else is wrong. (Same failure class as a version-pinned numba/UMAP pickle.)
Ref: https://stackoverflow.com/questions/77280192

## Recommended path: build from the Kimina Dockerfile

The Kimina Lean Server ships a Dockerfile whose `setup.sh` installs Lean + repl + mathlib4
at a pinned version. Easiest correct route:

```bash
git clone https://github.com/project-numina/kimina-lean-server
cd kimina-lean-server
# pick the Lean version that matches your prover model's proofs (see below)
docker build --build-arg=LEAN_SERVER_LEAN_VERSION=v4.21.0 -t pudding-lean .
```

Then wrap as a Modal image:

```python
lean_image = modal.Image.from_dockerfile("kimina-lean-server/Dockerfile",
                                          build_args={"LEAN_SERVER_LEAN_VERSION": "v4.21.0"})
# or, if you push to a registry:
# lean_image = modal.Image.from_registry("youruser/pudding-lean:latest")
```

Crib the whole three-runtime wiring from the Modal case study:
- blog: https://modal.com/blog/building-an-rl-theorem-proving-workflow-on-modal
- code: https://github.com/agencyenterprise/modal-rl-theorem-case-study

## DIY path (if not using Kimina's Dockerfile)

```dockerfile
# 1. elan (Lean toolchain manager)
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
# 2. a Mathlib project pinned to a CI-cached toolchain
#    lakefile + lean-toolchain MUST match Mathlib's pin exactly
RUN cd /mathlib-proj && lake exe cache get   # pulls prebuilt .olean — minutes, not hours
RUN cd /mathlib-proj && lake build           # should be a near-no-op if cache hit
# 3. leanprover-community/repl, built against the same toolchain
# 4. (optional) kimina-lean-server on top for the pooled REST API
```

## Runtime knobs

- `LEAN_SERVER_MAX_REPL_MEM` (default `8g`) — per-REPL OOM is real; raise for big proofs.
- File-descriptor limits bite at high `max_workers` (one REPL per core).

## Modal Volume file cap

The v1 Modal Volume caps at **500k files**. An unpacked Mathlib `.olean` cache or a
multi-million-file proof corpus (e.g. OProver's OProofs) can hit it — bake the built
Mathlib into an image *layer* rather than a Volume where possible, and pack a corpus
into a few large files (a single SQLite or FAISS index / tarball) or use the
no-file-limit **v2 Volume** (still in beta). Sizing/packing pain unverified — confirm
when you stash the cache.

## Match the toolchain to the model

DeepSeek-Prover-V2 and Goedel-Prover-V2 may target different Mathlib snapshots. If the
verifier's Mathlib is newer/older than what the model trained on, valid-looking proofs
fail to compile for spurious reasons (renamed lemmas, moved namespaces). Check each
model card for its Lean/Mathlib version and pin the image to match. **Verify this before
blaming the prover.**

## Unverified — confirm during build

- On-disk size of the `.olean` cache (no primary source; "a few GB" cited). Sizes the Volume.
- Whether `lake exe cache get` hits cleanly for your chosen toolchain (time the build — minutes = good, hours = pin mismatch).
- `@app.cls` (warm) vs `modal.Sandbox`-per-proof (isolated). Start warm; isolate if a proof wedges the server.
