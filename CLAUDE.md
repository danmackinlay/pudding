# pudding — project instructions

## Running Python / tooling

- Always invoke Python (and any project tooling) through direnv + uv. NEVER use system python. The canonical form is:

  ```
  direnv exec . uv run python <args>
  ```

  e.g. `direnv exec . uv run python eval.py`.

- This loads env vars from `.envrc`/`.env` (the `FEATHERLESS_API_KEY` etc.) and uses the project's `.venv` (Python 3.11) rather than the machine's pyenv default (3.14, too new for some deps).

- Never call `python`/`python3`/`pip` directly.

- Never add `python-dotenv`/`load_dotenv()` to code — env vars are managed by direnv only.

> Note for the human reader: Dan, running from his own shell, can omit the `direnv exec .` prefix because direnv auto-loads on `cd`; the agent cannot, so the agent must always include it.
