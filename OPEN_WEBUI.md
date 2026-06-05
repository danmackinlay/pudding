# Open WebUI — the solver **workbench**

The interactive UI for the swappable solver. Open WebUI is a polished OpenAI-compatible chat
frontend; we point it at our **shim** (`shim.py`), which runs the actual engine and streams a
**checked, confidence-rated** answer. OWUI just renders — it never runs code, so the repo's
separable roles stay intact.

The model menu is the **trust ladder** (`contenders.jsonl`, slugified): each entry is a
`(model × rung)`, and swapping the dropdown is climbing the ladder. The rungs:

| rung | id suffix | what it does | trust surface |
|------|-----------|--------------|---------------|
| CoT        | `…-cot`         | one streamed chat pass → last `\boxed{}` | — |
| self-verify| `…-self-verify` | CoT + a critique-and-restate pass | `✓ confirmed` / `⚠ corrected a→b` *(experimental — can over-correct)* |
| maj@k-deep | `…-deep`        | k seeded chains, majority vote (reuses `eval.solve_graded_async`) | **`answer · agreement m/k`** — the trustworthy surface |
| TIR        | `…-tir`         | the kernel-executing specialist (`​```python​`), bridged async | executed `​```output​` |

Override the lineup with `WORKBENCH_LINEUP=path.jsonl`; tune `WORKBENCH_DEEP_K` (default 5) and
`WORKBENCH_MAX_TOKENS` (default 16384). Cost in the footer comes from `prices.json` (static
output $/M; refresh from OpenRouter when it drifts). Per the first audition (`FINDINGS.md`), lead
with `…-deep` for confidence (maj@k agreement was 100% wherever a model answered); `self_verify`
*degraded* accuracy, so treat its verdict as experimental; the `…-tir` specialist scored 0% and
is kept only as the demoted incumbent.

## Run it (two processes)

**1. Start the shim** (owns the engine + picker; talks to OpenRouter / Featherless):
```
direnv exec . uv run uvicorn shim:app --port 8000
```
Check the lineup it built: `curl -s localhost:8000/v1/models | jq '.data[].id'`.

**2. Start Open WebUI — native via `uvx` (recommended), not Docker:**
```
WEBUI_AUTH=False \
OPENAI_API_BASE_URLS=http://localhost:8000/v1 \
OPENAI_API_KEYS=dummy \
ENABLE_OLLAMA_API=False \
ENABLE_EVALUATION_ARENA_MODELS=False \
ENABLE_TITLE_GENERATION=False \
ENABLE_TAGS_GENERATION=False \
ENABLE_AUTOCOMPLETE_GENERATION=False \
ENABLE_FOLLOW_UP_GENERATION=False \
ENABLE_RETRIEVAL_QUERY_GENERATION=False \
ENABLE_SEARCH_QUERY_GENERATION=False \
  uvx --python 3.11 open-webui@latest serve --port 8080
```
Then open **http://localhost:8080** and pick a rung in the model menu (e.g.
**`deepseek-v4-pro-cot`** for a fast stream, **`deepseek-v4-pro-deep`** for a voted answer).

**Why the extra flags** (all `ConfigVar`s, env-driven per launch — no DB reset needed):
- `ENABLE_OLLAMA_API=False` + `ENABLE_EVALUATION_ARENA_MODELS=False` — otherwise OWUI
  auto-discovers a local **Ollama** install and clutters the model menu with its models
  (embeddings, etc.) plus an "Arena Model". We want only the workbench rungs.
- The `ENABLE_*_GENERATION=False` set disables OWUI's **background task calls** (auto title,
  tags, autocomplete, follow-ups, query rewriting). Those fire extra `chat.completions`
  requests — which our shim would otherwise run the *full math solver* on (slow, wrong, and
  the cause of a spurious `Model '' was not found`). With them off, OWUI only sends real
  user turns to the solver. (The shim also answers an unknown id with a plain "unknown model"
  message instead of 500-ing, so a stray probe never hangs the UI.)

- Runs OWUI as a host process in its **own isolated uv env** (never the `pudding` venv —
  `uvx` handles isolation). Because it's native, it reaches the shim at plain
  `http://localhost:8000/v1` — no Docker `host.docker.internal` dance.
- `WEBUI_AUTH=False` = no-login local play surface. The `OPENAI_*` env vars pre-wire the
  connection, so the model just appears (or set it manually in **Admin → Settings →
  Connections → OpenAI**, base URL `http://localhost:8000/v1`, any key).
- **Python pin:** OWUI 0.9.6 requires `>=3.11,<3.13`; `--python 3.11` satisfies it. Re-check
  if you bump OWUI (it's version-picky).
- First run pulls OWUI + deps (hundreds of MB into the uv cache) — slow once, then cached.
- Data/persistence: `~/.open-webui` (override with `$DATA_DIR`); delete it to reset.

## What you should see
Prose with rendered LaTeX (`\(…\)`, `\boxed{…}`), streamed token-by-token, ending in a
`— {rung} · ⏱ Ns · N tok · ~$0.00` footer. Then, per rung:
- **`…-cot`** — just the streamed derivation + boxed answer.
- **`…-self-verify`** — a `**Verification pass**` separator, then a `✓ confirmed` / `⚠ corrected
  a→b` line above the footer.
- **`…-deep`** — a `⏳ running k samples…` status (no token stream — you can't stream a vote),
  then the voted answer as **`answer · agreement m/k`**.
- **`…-tir`** — a `​```python​` block and an injected `​```output​` block with the real kernel
  result (the only rung that executes code).

Models that expose a reasoning trace stream it into a **collapsible "thinking" block** (the
shim sends it on the `reasoning_content` channel) — handy when the visible answer is terse and
the derivation lives in the reasoning. Streaming makes the wait legible (a hard problem on a
generalist takes ~10–60s; `…-deep` is k× the calls, so slower).

## Docker / OrbStack fallback
If you prefer a container (loses native localhost reach, needs `host.docker.internal`):
```
docker run -d -p 3000:8080 \
  -e WEBUI_AUTH=False \
  -e OPENAI_API_BASE_URLS=http://host.docker.internal:8000/v1 \
  -e OPENAI_API_KEYS=dummy \
  -v open-webui:/app/backend/data --name open-webui \
  ghcr.io/open-webui/open-webui:main
```
(On Linux add `--add-host=host.docker.internal:host-gateway`.) UI at http://localhost:3000.

## Trade-offs (why bareback)
Native `uvx` is fastest to a working UI and dodges Docker networking; it loses Docker's
pinned-env reproducibility and is Python-version-picky. No functional loss for us — the
shim owns execution + rendering, OWUI is a pure display layer.
