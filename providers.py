"""Provider registry — the model-server axis of the audition (model × strategy × provider).

Maps a short provider name to an OpenAI-compatible client plus the one capability that
actually matters for test-time scaling: whether it supports server-side `n>1` (k samples
from one request, prompt prefill shared — the only cheap maj@k). base_url and API keys are
env-overridable (`<PROVIDER>_BASE_URL`, the listed key vars) so you can point at anything
without editing code.

CONFIRMED base URLs: featherless, novita, selfhost (= SOLVER_BASE_URL).
LIKELY — verify on first 401/404, all overridable: moonshot (Kimi K2.6), openrouter,
deepinfra. See PLAN.md §6.1 for the n>1 / cost table this encodes.
"""
import os
from dataclasses import dataclass

from openai import AsyncOpenAI, OpenAI


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    key_envs: tuple[str, ...]      # tried in order; first one set wins ("EMPTY" for self-host)
    supports_n: bool = False       # server-side parallel sampling (maj@k in ONE request)
    notes: str = ""

    def api_key(self) -> str:
        for e in self.key_envs:
            v = os.environ.get(e)
            if v:
                return v
        return "EMPTY"             # self-hosted vLLM accepts any key

    def base(self) -> str:
        return os.environ.get(f"{self.name.upper()}_BASE_URL", self.base_url)


PROVIDERS: dict[str, Provider] = {
    # flat-rate; chat `n` not supported; maj@k bounded by the plan's concurrency limit.
    "featherless": Provider(
        "featherless", "https://api.featherless.ai/v1",
        ("FEATHERLESS_API_KEY", "SOLVER_API_KEY"),
        supports_n=False, notes="specialists live here; flat-rate; n=1; concurrency-capped"),
    # per-token; exposes n<=128; also hosts DeepSeek-Prover-V2-671B (Phase C).
    "novita": Provider(
        "novita", "https://api.novita.ai/openai", ("NOVITA_API_KEY",),
        supports_n=True, notes="per-token; n<=128; hosts DeepSeek-Prover-V2-671B"),
    # Kimi K2.6 first-party; OpenAI-compatible; 'thinking' mode → reasoning_content.
    "moonshot": Provider(
        "moonshot", "https://api.moonshot.ai/v1", ("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        supports_n=False, notes="Kimi K2.6; OpenAI-compatible; thinking→reasoning_content"),
    # router; n silently dropped to 1; set the data-policy filter to no-train if it matters.
    "openrouter": Provider(
        "openrouter", "https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",),
        supports_n=False, notes="router; n→1; pin data policy for no-train routing"),
    # per-token fountain hosting many open generalists (Kimi K2.6, Qwen3, DeepSeek).
    "deepinfra": Provider(
        "deepinfra", "https://api.deepinfra.com/v1/openai", ("DEEPINFRA_API_KEY",),
        supports_n=True, notes="per-token; hosts Kimi K2.6 / Qwen3 / DeepSeek"),
    # local Ollama OpenAI-compat endpoint (set up later, e.g. a local Nemotron). No key needed.
    "ollama": Provider(
        "ollama", "http://localhost:11434/v1", ("OLLAMA_API_KEY",),
        supports_n=False, notes="local Ollama OpenAI-compat (e.g. Nemotron); no key; n unsupported"),
    # self-hosted vLLM/SGLang (serve.py): the only path with n>1 AND shared-prefill saving.
    "selfhost": Provider(
        "selfhost", os.environ.get("SOLVER_BASE_URL", "http://localhost:8000/v1"),
        ("VLLM_KEY", "SOLVER_API_KEY"),
        supports_n=True, notes="vLLM serve.py; n>1 with shared prefill — cheapest wide maj@k"),
}


def get_provider(provider: str | None) -> Provider:
    """Resolve a provider name. `None` reproduces the repo's pre-pivot default: self-host if
    SOLVER_BASE_URL is set, else Featherless (so the verified solver path is unchanged)."""
    if provider is None:
        provider = "selfhost" if os.environ.get("SOLVER_BASE_URL") else "featherless"
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(PROVIDERS)}")
    return PROVIDERS[provider]


def make_client(provider: str | None = None) -> OpenAI:
    """An OpenAI-compatible client for `provider` (None → the back-compat default above).

    A finite per-request `timeout` (PUDDING_HTTP_TIMEOUT, default 180s) is the real cap on a
    hung/slow call: eval's per-problem ThreadPoolExecutor timeout can't hard-stop one, because
    exiting its `with` block waits on the orphaned request (a thinking-model call once ran 146s
    past a 120s cap). Bound it at the HTTP layer instead.
    """
    p = get_provider(provider)
    timeout = float(os.environ.get("PUDDING_HTTP_TIMEOUT", "180"))
    return OpenAI(base_url=p.base(), api_key=p.api_key(), timeout=timeout, max_retries=2)


def make_async_client(provider: str | None = None) -> AsyncOpenAI:
    """Async sibling of make_client — native async IO so the audition runs k samples / many
    problems concurrently on one event loop (no thread per call). Same timeout policy."""
    p = get_provider(provider)
    timeout = float(os.environ.get("PUDDING_HTTP_TIMEOUT", "180"))
    return AsyncOpenAI(base_url=p.base(), api_key=p.api_key(), timeout=timeout, max_retries=2)
