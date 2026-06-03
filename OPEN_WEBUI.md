# Open WebUI — the solver chat surface

The interactive UI for the TIR solver. Open WebUI is a polished OpenAI-compatible chat
frontend; we point it at our **shim** (`shim.py`), which runs the actual TIR loop (model +
kernel) and streams the *executed* transcript. OWUI just renders — it never runs code, so
the repo's three separable roles stay intact. maj@k is **not** here (it's batch:
`eval.py --k`, `fanout.py`).

## Run it (two processes)

**1. Start the shim** (owns the loop + kernel; talks to Featherless):
```
direnv exec . uv run uvicorn shim:app --port 8000
```

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
Then open **http://localhost:8080** and pick **`tir-solver`** in the model menu.

**Why the extra flags** (all `ConfigVar`s, env-driven per launch — no DB reset needed):
- `ENABLE_OLLAMA_API=False` + `ENABLE_EVALUATION_ARENA_MODELS=False` — otherwise OWUI
  auto-discovers a local **Ollama** install and clutters the model menu with its models
  (embeddings, etc.) plus an "Arena Model". We want only `tir-solver`.
- The `ENABLE_*_GENERATION=False` set disables OWUI's **background task calls** (auto title,
  tags, autocomplete, follow-ups, query rewriting). Those fire extra `chat.completions`
  requests — which our shim would otherwise run the *full math solver* on (slow, wrong, and
  the cause of a spurious `Model '' was not found`). With them off, OWUI only sends real
  user turns to the solver.

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
Prose with rendered LaTeX (`\(…\)`, `\boxed{…}`), a `​```python​` block, an injected
`​```output​` block with the real result, the final boxed answer, and a `⏱ Ns · N tok`
footer. Tokens stream live (the model is ~28 tok/s on Featherless, so a problem takes
~10–40s — streaming makes the wait legible).

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
