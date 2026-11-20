"""Content fetching with a deterministic fallback ladder and quality gating.

Order: plain HTTP -> readability extraction -> Playwright (if installed) ->
reader API (if enabled). A document is only accepted when the extracted body
passes both a length gate and a topic-relevance gate, because a failed
extraction otherwise leaks unrelated headlines into the evidence set.
"""

from __future__ import annotations

import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from backend.config import settings
from backend.core.models import Document, SearchHit
from backend.providers.ratelimit import CACHE, LIMITER
from backend.providers.readability import extract_main, looks_relevant
from backend.providers.search import SESSION, sanitize_url

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# Binary/content types we must not try to parse as an article.
_SKIP_SUFFIX = (".pdf", ".zip", ".rar", ".doc", ".docx", ".xls", ".xlsx",
                ".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mp3")

_PRIMARY_HINTS = ("gov.cn", ".gov", "stats.", "caict.ac.cn", "miit.gov.cn",
                  "pbc.gov.cn", "csrc.gov.cn", "idc.com", "gartner.com", "oecd.org")


def _is_primary(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(h in host for h in _PRIMARY_HINTS)


class FetchChain:
    def __init__(self) -> None:
        self._last_fetch: dict[str, float] = defaultdict(float)
        self._robots_cache: dict[str, str] = {}
        self._robots_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.stats = defaultdict(int)
        # DeepSeek Fetch returns server-side rendered Markdown, which is the
        # only way past the JS-heavy / anti-bot Chinese pages that kept ending
        # up as "too_short" on direct HTTP and Playwright.
        self._deepseek = None
        if settings.deepseek_api_key:
            try:
                from backend.providers.search import DeepSeekSearchProvider

                self._deepseek = DeepSeekSearchProvider(
                    settings.deepseek_api_key, settings.deepseek_base_url)
            except Exception:
                self._deepseek = None

        # Tavily Extract, for users whose only key is Tavily. No extra gate
        # here: the provider itself validates the key and the live-provider
        # switch, so there is exactly one place that decides.
        self._tavily = None
        try:
            from backend.providers.search import TavilyProvider

            self._tavily = TavilyProvider()
        except Exception:
            self._tavily = None

    def _deepseek_fetch(self, url: str) -> str:
        if not self._deepseek:
            return ""
        result = self._deepseek.fetch_page(url)
        if not result:
            return ""
        _, content = result
        return content

    def _tavily_fetch(self, url: str) -> str:
        """Tavily Extract, for users whose only key is Tavily.

        Without it their fallback chain dead-ends: Playwright is an optional
        heavy install, and the reader API (r.jina.ai) is blocked by this
        network's outbound proxy (measured: tunnel 502), so every anti-bot page
        degraded to a SERP snippet.
        """
        if not self._tavily:
            return ""
        result = self._tavily.extract(url)
        if not result:
            return ""
        _, content = result
        return content

    def _bump(self, key: str) -> None:
        """Thread-safe counter increment (fetch_many runs in a pool)."""
        with self._stats_lock:
            self.stats[key] += 1

    # -- politeness ----------------------------------------------------------
    def _robots_allowed(self, url: str) -> bool:
        if not settings.respect_robots:
            return True
        parts = urlsplit(url)
        if not parts.netloc:
            return False
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            if origin in self._robots_cache:
                robots = self._robots_cache[origin]
            else:
                try:
                    req = urllib.request.Request(origin + "/robots.txt", headers=_HEADERS)
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        robots = resp.read().decode("utf-8", "ignore")
                except Exception:
                    robots = ""
                self._robots_cache[origin] = robots
        # Conservative reading: a blanket disallow blocks the crawl.
        return not re.search(r"^\s*Disallow:\s*/\s*$", robots, re.M)

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = settings.per_domain_delay_s - (time.time() - self._last_fetch[host])
        if wait > 0:
            time.sleep(wait)
        self._last_fetch[host] = time.time()

    # -- fetching ------------------------------------------------------------
    def _http_get(self, url: str) -> str:
        # Reuse the shared cookie jar: some Chinese portals gate first requests
        # behind a cookie challenge that a plain urlopen cannot satisfy.
        try:
            return SESSION.get(url, timeout=settings.fetch_timeout_s)
        except Exception:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=settings.fetch_timeout_s) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
                    raise ValueError(f"non-html content-type: {ctype}")
                return resp.read().decode("utf-8", "ignore")

    def _playwright(self, url: str) -> str:
        if not settings.playwright_fallback:
            return ""
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except ImportError:
            return ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(user_agent=_UA)
                page.goto(url, timeout=int(settings.fetch_timeout_s * 1000),
                          wait_until="domcontentloaded")
                page.wait_for_timeout(1200)
                content = page.content()
                browser.close()
            _, body = extract_main(content)
            return body
        except Exception:
            return ""

    def _reader_api(self, url: str) -> str:
        if not settings.reader_api_fallback:
            return ""
        try:
            req = urllib.request.Request("https://r.jina.ai/" + url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=settings.fetch_timeout_s) as resp:
                return resp.read().decode("utf-8", "ignore").strip()
        except Exception:
            return ""

    # -- public API ----------------------------------------------------------
    def fetch(self, hit: SearchHit, keywords: list[str] | None = None) -> Document | None:
        url = sanitize_url(hit.url)
        if not url:
            self._bump("bad_url")
            return None
        if urlsplit(url).path.lower().endswith(_SKIP_SUFFIX):
            self._bump("skip_binary")
            return None

        # Cache hit: avoids re-downloading across runs and across retries after
        # a search-engine rate limit forced a restart.
        cache_key = CACHE.make_key("doc", url)
        cached = CACHE.get(cache_key)
        if cached and isinstance(cached, dict):
            doc = Document(
                url=url,
                title=cached.get("title") or hit.title,
                raw_text=cached.get("raw_text", ""),
                published_at=cached.get("published_at"),
                fetch_method=cached.get("fetch_method", "cache"),
                source_type=cached.get("source_type", "secondary"),
            )
            self._bump("cache_hit")
            if keywords and not looks_relevant(doc.raw_text, keywords):
                self._bump("irrelevant")
                return None
            return doc

        if not self._robots_allowed(url):
            self._bump("robots_blocked")
            return None

        LIMITER.wait(url, extra=settings.per_domain_delay_s - 1.0)
        title, body, method = "", "", "http"
        try:
            html = self._http_get(url)
            title, body = extract_main(html)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            self._bump("http_error")

        if len(body) < settings.min_body_chars and self._deepseek:
            # Before Playwright: server-side rendering elsewhere is both faster
            # and unaffected by this machine's browser/anti-bot problems.
            alt = self._deepseek_fetch(url)
            if len(alt) > len(body):
                body, method = alt, "deepseek_fetch"
        if len(body) < settings.min_body_chars and self._tavily:
            alt = self._tavily_fetch(url)
            if len(alt) > len(body):
                body, method = alt, "tavily_extract"
        if len(body) < settings.min_body_chars and settings.playwright_fallback:
            alt = self._playwright(url)
            if len(alt) > len(body):
                body, method = alt, "playwright"
        if len(body) < settings.min_body_chars and settings.reader_api_fallback:
            alt = self._reader_api(url)
            if len(alt) > len(body):
                body, method = alt, "reader_api"

        if len(body) < settings.min_body_chars:
            self._bump("too_short")
            return None
        if keywords and not looks_relevant(body, keywords):
            # Body extracted cleanly but is about a different topic. Rejecting
            # here is what keeps unrelated headlines out of the evidence set.
            self._bump("irrelevant")
            return None

        self._bump("ok:" + method)
        doc = Document(
            url=url,
            title=(title or hit.title)[:200],
            raw_text=body[:12000],
            published_at=_guess_date(hit.snippet, body),
            fetch_method=method,
            source_type="primary" if _is_primary(url) else "secondary",
        )
        CACHE.put(cache_key, {
            "title": doc.title,
            "raw_text": doc.raw_text,
            "published_at": doc.published_at,
            "fetch_method": doc.fetch_method,
            "source_type": doc.source_type,
        })
        return doc

    def fetch_many(
        self,
        items: list[tuple[SearchHit, list[str]]],
        max_workers: int = 6,
    ) -> list[Document | None]:
        """Fetch concurrently. The rate limiter still paces each domain, so
        politeness holds while different sites download in parallel.

        This is the main wall-clock win: a serial pass over 15 Chinese news
        pages took ~100s; parallel fetching keeps per-host gaps identical but
        overlaps the wait across hosts.
        """
        if not items:
            return []
        if len(items) == 1 or max_workers <= 1:
            return [self.fetch(h, keywords=kw) for h, kw in items]

        def work(pair: tuple[SearchHit, list[str]]) -> Document | None:
            hit, kw = pair
            try:
                return self.fetch(hit, keywords=kw)
            except Exception:
                self._bump("exception")
                return None

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(work, items))


_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def _guess_date(snippet: str, body: str) -> str | None:
    for source in (snippet or "", body[:1500]):
        m = _DATE_RE.search(source)
        if m:
            y, mo, d = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


__all__ = ["FetchChain"]
