"""Configuration loaded from environment variables and the project's .env file.

All providers fall back to the offline adapter when their API key is absent,
so the entire project runs with zero external services out of the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from the project's .env into os.environ.

    Deliberately dependency-free (no python-dotenv). Without this step the
    documented workflow was broken: `cp deploy/.env.example .env` produced a
    file that nothing ever read, so every key silently fell back to its default
    and the user's configuration appeared to be ignored.

    Real environment variables always win (setdefault), so a shell export is
    never overridden by the file.
    """
    path = path or (Path(__file__).resolve().parent.parent / ".env")
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip one layer of matching quotes, keep everything else verbatim.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Must run before the class body below: dataclass field defaults call os.getenv
# at class-definition time.
_load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


_DEEPSEEK_HOSTS = ("api.deepseek.com", "api.deepseek.cn")


def _deepseek_search_key() -> str:
    """Key used for DeepSeek's native Search/Fetch, or "" when unavailable.

    Deliberately NOT an unconditional fallback to the LLM key.  Measured
    2026-11-20: the LLM key supplied for this project belongs to an
    OpenAI-compatible MaaS gateway (maas.qianwenaiapi.com). It answers
    /chat/completions with 200 but returns 404 for /v1/tools/search and
    /v1/tools/fetch, because that gateway proxies the model only.

    The previous code borrowed the LLM key unconditionally, so any non-DeepSeek
    key installed a DeepSeekSearchProvider at the head of the engine chain that
    was guaranteed to 404 on every single run - wasting the first attempt of
    every query and masking the real failure behind a bogus "deepseek: 404".

    Borrowing is therefore conditional on the configured LLM endpoint actually
    being DeepSeek's own API.
    """
    explicit = os.getenv("RESEARCHMIND_DEEPSEEK_API_KEY", "").strip()
    if explicit:
        return explicit
    base = os.getenv("RESEARCHMIND_LLM_BASE_URL", "https://api.deepseek.com/v1").lower()
    if any(host in base for host in _DEEPSEEK_HOSTS):
        return os.getenv("RESEARCHMIND_LLM_API_KEY", "").strip()
    return ""


@dataclass(frozen=True)
class Settings:
    # LLM: any OpenAI-compatible endpoint (DeepSeek, Qwen, OpenAI, vLLM)
    llm_api_key: str = os.getenv("RESEARCHMIND_LLM_API_KEY", "")
    llm_base_url: str = os.getenv("RESEARCHMIND_LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_model: str = os.getenv("RESEARCHMIND_LLM_MODEL", "deepseek-chat")
    # Search: Tavily or a compatible provider
    search_api_key: str = os.getenv("RESEARCHMIND_SEARCH_API_KEY", "")
    search_base_url: str = os.getenv("RESEARCHMIND_SEARCH_BASE_URL", "https://api.tavily.com")
    search_max_results: int = int(os.getenv("RESEARCHMIND_SEARCH_MAX_RESULTS", "6"))

    # DeepSeek native Search/Fetch (api.deepseek.com/v1/tools/*, 2026-10-17).
    # Empty unless an explicit DeepSeek key is set, or the LLM endpoint itself is
    # DeepSeek's API - see _deepseek_search_key() for why the blind fallback was
    # removed.
    deepseek_api_key: str = _deepseek_search_key()
    deepseek_base_url: str = os.getenv("RESEARCHMIND_DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Reasoning models such as deepseek-v4.1-flash emit a long reasoning_content
    # preamble before any content. With a small budget they return
    # finish_reason="length" and content="" - which downstream looks exactly like
    # "the model found nothing". Keep this generous, and let llm.py escalate on
    # truncation.
    llm_max_tokens: int = int(os.getenv("RESEARCHMIND_LLM_MAX_TOKENS", "4000"))
    llm_timeout_s: float = float(os.getenv("RESEARCHMIND_LLM_TIMEOUT", "120"))

    # Fetching
    fetch_timeout_s: float = float(os.getenv("RESEARCHMIND_FETCH_TIMEOUT", "15"))
    min_body_chars: int = int(os.getenv("RESEARCHMIND_MIN_BODY_CHARS", "200"))
    playwright_fallback: bool = _bool("RESEARCHMIND_PLAYWRIGHT_FALLBACK", True)
    reader_api_fallback: bool = _bool("RESEARCHMIND_READER_API_FALLBACK", True)
    respect_robots: bool = _bool("RESEARCHMIND_RESPECT_ROBOTS", True)
    per_domain_delay_s: float = float(os.getenv("RESEARCHMIND_DOMAIN_DELAY", "1.0"))

    # Extraction without an LLM key: deterministic sentence scoring.
    use_heuristic_extractor: bool = _bool("RESEARCHMIND_HEURISTIC_EXTRACT", True)
    max_facts_per_doc: int = int(os.getenv("RESEARCHMIND_MAX_FACTS_PER_DOC", "5"))
    fact_min_score: int = int(os.getenv("RESEARCHMIND_FACT_MIN_SCORE", "30"))

    # Live web research (default ON): real search + real page fetching.
    # Disable only for offline demos and tests.
    use_web: bool = _bool("RESEARCHMIND_USE_WEB", True)

    # When a page body cannot be fetched (JS-gated, robots-blocked, too short),
    # fall back to the search-engine snippet for that URL. The snippet is text
    # the engine indexed from the page, so quotes stay verbatim; such documents
    # are tagged fetch_method="serp_snippet" and disclosed in the report.
    snippet_fallback: bool = _bool("RESEARCHMIND_SNIPPET_FALLBACK", True)

    # Storage
    database_url: str = os.getenv(
        "RESEARCHMIND_DATABASE_URL", "sqlite+aiosqlite:///./researchmind.db"
    )
    redis_url: str = os.getenv("RESEARCHMIND_REDIS_URL", "")

    # Runtime
    use_live_providers: bool = _bool("RESEARCHMIND_USE_LIVE_PROVIDERS", True)
    log_level: str = os.getenv("RESEARCHMIND_LOG_LEVEL", "INFO")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and self.use_live_providers

    @property
    def search_enabled(self) -> bool:
        return bool(self.search_api_key) and self.use_live_providers

    @property
    def deepseek_search_enabled(self) -> bool:
        return bool(self.deepseek_api_key) and self.use_live_providers


settings = Settings()
