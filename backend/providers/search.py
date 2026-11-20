"""Key-free web search providers with session cookies and engine fallback.

Findings from live testing on this machine (2026-11-20):
- so.com (360) returns relevant Chinese results and exposes the real
  destination URL in `data-mdurl` plus a snippet in `data-result-summary`.
  Its `href` is an opaque JS redirect stub, so `data-mdurl` must be used.
- so.com rate-limits anonymous requests and serves a 3 KB captcha page. A
  warm-up request that seeds a cookie jar, plus backoff, makes it reliable.
- Bing HTML returns generic navigation results for these queries.
- Google / DuckDuckGo / Wikipedia are blocked by the outbound proxy (502).
- Tavily works when a key is supplied and is preferred in that case.
"""

from __future__ import annotations

import html as _html
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from xml.etree import ElementTree

from backend.config import settings
from backend.core.models import SearchHit
from backend.providers.ratelimit import CACHE, LIMITER

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HEADERS = [
    ("User-Agent", _UA),
    ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
    ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
]


class SearchError(Exception):
    """Raised when an engine cannot produce results."""


class RateLimitedError(SearchError):
    """Raised when the engine explicitly throttled us (captcha/challenge page).

    Distinct from a generic failure because it must NOT be retried inside the
    same run: retrying a throttled host only extends the throttle. Observed
    cost of the earlier design (30s penalty x 3 attempts x 14 queries) was a
    multi-minute hang with zero results.
    """


_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")


def sanitize_url(url: str) -> str:
    """Reject or repair URLs harvested from live SERP HTML.

    Real search pages contain relative targets ("/s?q=..."), unencoded CJK and
    raw whitespace. urllib raises InvalidURL on those, which used to abort an
    entire research run. Returns '' for anything that is not an absolute
    http(s) target, otherwise a percent-encoded, fetchable URL.
    """
    if not url:
        return ""
    url = _CTRL_RE.sub("", url.strip())
    url = _html.unescape(url)
    if not url.lower().startswith(("http://", "https://")):
        return ""
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc or "." not in parts.netloc:
        return ""
    safe_path = urllib.parse.quote(parts.path, safe="/%~+._-")
    safe_query = urllib.parse.quote(parts.query, safe="/%&=+._-")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, safe_path, safe_query, "")
    )


class HttpSession:
    """Shared cookie jar so providers keep a warm, non-throttled session."""

    def __init__(self) -> None:
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )
        self._opener.addheaders = list(_HEADERS)
        self._warmed: set[str] = set()

    def warm(self, origin: str, delay: float = 1.5) -> None:
        if origin in self._warmed:
            return
        try:
            self.get(origin, timeout=10)
            self._warmed.add(origin)
            time.sleep(delay)
        except Exception:
            self._warmed.add(origin)

    def get(self, url: str, timeout: float = 15.0) -> str:
        with self._opener.open(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")


SESSION = HttpSession()


def _strip_tags(fragment: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


# ---------------------------------------------------------------------------
# 360 Search
# ---------------------------------------------------------------------------

_SO_BLOCK_RE = re.compile(r'<li class="res-list"', re.S)
_SO_MDURL_RE = re.compile(r'data-mdurl="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S)
_SO_HREF_RE = re.compile(r'<h3[^>]*>\s*<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S)
_SO_SUMMARY_RE = re.compile(r'data-result-summary[^>]*>(.*?)</span>', re.S)
_SO_DATE_RE = re.compile(r'<span class="gray[^"]*">(.*?)</span>', re.S)
_CAPTCHA_MARKERS = ("captcha", "安全验证", "wappass", "请输入验证码")
# Search-engine internal links (SERP self-links, ranking portals, ad wrappers).
# These are not articles; fetching them yields another results page and wasted
# budget, and they once carried raw CJK that crashed urllib.
_INTERNAL_RE = re.compile(
    r"(?:^|\.)(so\.com|baidu\.com|bing\.com|sogou\.com|sm\.cn|"
    r"ranks\.hao\.360\.cn|ai\.so\.com|news\.so\.com)(?:/|$)", re.I)


class SoSearchProvider:
    """360 Search scraper with cookie session, warm-up and backoff."""

    name = "so.com"
    ORIGIN = "https://www.so.com"
    # How long to skip this engine entirely after it throttles us.
    CIRCUIT_COOLDOWN_S = 60.0

    def __init__(self) -> None:
        self.backoff = 0.0
        self._circuit_open_until = 0.0

    def _request(self, query: str, page: int) -> str:
        url = self.ORIGIN + "/s?" + urllib.parse.urlencode({"q": query, "pn": page})
        return SESSION.get(url, timeout=15.0)

    def _parse(self, html_text: str, limit: int) -> list[SearchHit]:
        if len(html_text) < 20000 and any(m in html_text.lower() for m in _CAPTCHA_MARKERS):
            raise RateLimitedError("so.com served a captcha page (rate limited)")
        if len(html_text) < 20000 and _SO_BLOCK_RE.search(html_text) is None:
            raise SearchError("so.com returned a short page with no result blocks")

        hits: list[SearchHit] = []
        seen: set[str] = set()
        blocks = re.split(_SO_BLOCK_RE, html_text)[1:]
        for block in blocks:
            match = _SO_MDURL_RE.search(block) or _SO_HREF_RE.search(block)
            if not match:
                continue
            title = _strip_tags(match.group(2))
            # Prefer data-mdurl (the true destination) over href, which is often
            # an opaque JS redirect stub. Sanitize fixes raw CJK / relative
            # targets; drop search-engine internal links (not articles).
            cleaned = sanitize_url(match.group(1))
            if not cleaned:
                continue
            if _INTERNAL_RE.search(urllib.parse.urlsplit(cleaned).netloc):
                continue
            url = cleaned
            if url in seen or not title:
                continue
            snippet = ""
            sm = _SO_SUMMARY_RE.search(block)
            if sm:
                snippet = _strip_tags(sm.group(1))
            dm = _SO_DATE_RE.search(block)
            if dm:
                snippet = (snippet + " " + _strip_tags(dm.group(1))).strip()
            seen.add(url)
            hits.append(SearchHit(url=url, title=title, snippet=snippet[:400], source=self.name))
            if len(hits) >= limit:
                break
        return hits

    def search(self, query: str, limit: int = 10, retries: int = 2) -> list[SearchHit]:
        cache_key = CACHE.make_key("so", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            # Rehydrate from cache so a rate-limited run still completes.
            # Sub-question linkage is caller state, never cached: rebuilding it
            # from a list would corrupt the set-typed field downstream.
            return [
                SearchHit(
                    url=item["url"],
                    title=item["title"],
                    snippet=item.get("snippet", ""),
                    source=item.get("source", self.name),
                )
                for item in cached
                if isinstance(item, dict) and item.get("url")
            ]

        # Circuit breaker: while the host is throttling us, fail fast instead of
        # paying 30s per attempt for every remaining query. Retrying a throttled
        # host only extends the throttle, and it turned one run into a
        # multi-minute hang with zero results.
        if time.time() < self._circuit_open_until:
            raise RateLimitedError(
                f"so.com circuit open for {self._circuit_open_until - time.time():.0f}s"
            )

        SESSION.warm(self.ORIGIN)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                LIMITER.wait(self.ORIGIN, extra=self.backoff)
                html_text = self._request(query, page=1)
                hits = self._parse(html_text, limit)
                if hits:
                    self.backoff = 0.0
                    CACHE.put(cache_key, [
                        {"url": h.url, "title": h.title, "snippet": h.snippet,
                         "source": h.source}
                        for h in hits
                    ])
                    return hits
                last_error = SearchError("so.com returned no parseable results")
            except RateLimitedError as exc:
                # Throttled: open the circuit for the cooldown window and stop.
                self._circuit_open_until = time.time() + self.CIRCUIT_COOLDOWN_S
                self.backoff = 0.0
                LIMITER.penalize(self.ORIGIN, seconds=self.CIRCUIT_COOLDOWN_S)
                raise
            except (urllib.error.URLError, TimeoutError, ValueError, SearchError) as exc:
                last_error = exc
                self.backoff = min(self.backoff + 2.0, 8.0)
                LIMITER.penalize(self.ORIGIN, seconds=self.backoff)
        raise SearchError(f"so.com failed: {last_error}")


# ---------------------------------------------------------------------------
# Sogou
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_SOGOU_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
_SOGOU_A_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
# Inline JSON state, same hazard as Baidu's hydration payload.
_JSON_TAIL_RE = re.compile(r'\\?"\s*[}\]]|\\"|"\s*:\s*[\[{]')


def _visible_text(fragment: str) -> str:
    """Text a reader would see: scripts and styles removed before tags."""
    text = _SCRIPT_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", _strip_tags(text)).strip()


class SogouSearchProvider:
    """Sogou HTML search.

    Added because the chain could not cover a seven-dimension plan: measured
    2026-11-20, so.com served four queries and then captcha'd, baidu answered
    with an anti-bot stub, and Bing returned a fixed off-topic set, so only 3 of
    7 sub-questions got any source at all. Sogou answered three consecutive
    queries (408 KB / 506 KB / 503 KB) with on-topic direct links.

    Its links are mostly absolute URLs rather than redirect wrappers, so no
    per-hit resolution request is needed; the occasional `/link?url=` stub is
    resolved with one follow-up.
    """

    name = "sogou"
    ORIGIN = "https://www.sogou.com"
    # A throttled Sogou response is ~4 KB; a real SERP is 400-500 KB.
    STUB_LEN = 20_000
    CIRCUIT_COOLDOWN_S = 60.0
    _CAPTCHA = ("请输入验证码", "antispider", "安全验证", "访问过于频繁")

    def __init__(self) -> None:
        self.backoff = 0.0
        self._circuit_open_until = 0.0

    def _request(self, query: str) -> str:
        url = self.ORIGIN + "/web?" + urllib.parse.urlencode({"query": query})
        return SESSION.get(url, timeout=15.0)

    def _resolve(self, link: str) -> str:
        if link.startswith("/"):
            link = self.ORIGIN + link
        try:
            req = urllib.request.Request(link, headers=dict(_HEADERS))
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return sanitize_url(resp.geturl())
        except Exception:
            return ""

    def _parse(self, html_text: str, limit: int) -> list[SearchHit]:
        low = html_text.lower()
        if len(html_text) < self.STUB_LEN:
            if any(m in html_text for m in self._CAPTCHA) or "antispider" in low:
                raise RateLimitedError("sogou served a captcha page (rate limited)")
            raise SearchError("sogou returned a short page (throttled or empty SERP)")

        out: list[SearchHit] = []
        seen: set[str] = set()
        for chunk in re.split(r"(?=<h3)", html_text)[1:]:
            if len(out) >= limit:
                break
            head = _SOGOU_H3_RE.search(chunk)
            if not head:
                continue
            anchor = _SOGOU_A_RE.search(head.group(1))
            if not anchor:
                continue
            title = _visible_text(anchor.group(2))
            if len(title) < 5:
                continue
            href = _html.unescape(anchor.group(1))
            url = sanitize_url(href) if href.startswith("http") else ""
            if "/link?url=" in href or not url:
                url = self._resolve(href)
            if not url or url in seen:
                continue
            # Keep only article-like targets: no SERP self-links.
            if _INTERNAL_RE.search(urllib.parse.urlsplit(url).netloc):
                continue
            tail = _visible_text(chunk[head.end():])
            cut = _JSON_TAIL_RE.search(tail)
            if cut:
                tail = tail[: cut.start()]
            seen.add(url)
            out.append(SearchHit(url=url, title=title[:200],
                                 snippet=tail[:400], source=self.name))
        return out

    def search(self, query: str, limit: int = 10, retries: int = 1) -> list[SearchHit]:
        cache_key = CACHE.make_key("sogou", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            return [
                SearchHit(url=i["url"], title=i["title"], snippet=i.get("snippet", ""),
                          source=i.get("source", self.name))
                for i in cached if isinstance(i, dict) and i.get("url")
            ]

        if time.time() < self._circuit_open_until:
            raise RateLimitedError(
                f"sogou circuit open for {self._circuit_open_until - time.time():.0f}s"
            )

        SESSION.warm(self.ORIGIN)
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                LIMITER.wait(self.ORIGIN, extra=self.backoff)
                hits = self._parse(self._request(query), limit)
                if hits:
                    self.backoff = 0.0
                    CACHE.put(cache_key, [
                        {"url": h.url, "title": h.title, "snippet": h.snippet,
                         "source": h.source}
                        for h in hits
                    ])
                    return hits
                last_error = SearchError("sogou returned no parseable results")
            except RateLimitedError:
                self._circuit_open_until = time.time() + self.CIRCUIT_COOLDOWN_S
                self.backoff = 0.0
                LIMITER.penalize(self.ORIGIN, seconds=self.CIRCUIT_COOLDOWN_S)
                raise
            except (urllib.error.URLError, TimeoutError, ValueError, SearchError) as exc:
                last_error = exc
                self.backoff = min(self.backoff + 2.0, 8.0)
                LIMITER.penalize(self.ORIGIN, seconds=self.backoff)
        raise SearchError(f"sogou failed: {last_error}")


# ---------------------------------------------------------------------------
# Bing News RSS
# ---------------------------------------------------------------------------


class BingNewsProvider:
    """Bing News RSS fallback — provides recency rather than ranked pages.

    Observed behaviour behind this machine's proxy: Bing frequently answers the
    RSS endpoint with an HTML shell (`<!doctype html>`, ~195 KB) instead of a
    feed. Detecting that by `startswith("<")` is wrong — HTML starts with `<`
    too — so this checks for real feed markers and fails fast instead of
    burning retries on an XML parse error.
    """

    name = "bing-news"
    ORIGIN = "https://www.bing.com"

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        cache_key = CACHE.make_key("bing-news", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            return [
                SearchHit(url=i["url"], title=i["title"], snippet=i.get("snippet", ""),
                          source=i.get("source", self.name))
                for i in cached if isinstance(i, dict) and i.get("url")
            ]

        url = self.ORIGIN + "/news/search?" + urllib.parse.urlencode({
            "q": query, "format": "rss", "count": limit,
        })
        LIMITER.wait(self.ORIGIN)
        try:
            body = SESSION.get(url, timeout=15.0)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SearchError(f"bing-news failed: {exc}") from exc

        head = body.lstrip()[:400].lower()
        if "<rss" not in head and "<?xml" not in head and "<feed" not in head:
            raise SearchError("bing-news returned HTML instead of an RSS feed")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise SearchError(f"bing-news bad xml: {exc}") from exc

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = sanitize_url(item.findtext("link") or "")
            if not title or not link or link in seen:
                continue
            if _INTERNAL_RE.search(urllib.parse.urlsplit(link).netloc):
                continue
            desc = _strip_tags(item.findtext("description") or "")
            date = (item.findtext("pubDate") or "").strip()
            snippet = f"{date} — {desc}" if date else desc
            seen.add(link)
            hits.append(SearchHit(url=link, title=title, snippet=snippet[:400], source=self.name))
            if len(hits) >= limit:
                break
        if not hits:
            raise SearchError("bing-news returned no items")
        CACHE.put(cache_key, [
            {"url": h.url, "title": h.title, "snippet": h.snippet, "source": h.source}
            for h in hits
        ])
        return hits


# ---------------------------------------------------------------------------
# Tavily (preferred when keyed)
# ---------------------------------------------------------------------------


class TavilyProvider:
    """Tavily search + extract (free tier ~1000 credits/month).

    Error classification matters here: before it existed, every failure surfaced
    as a generic `SearchError("tavily failed: HTTP Error 401")`, so a wrong key
    was indistinguishable from a throttled engine and the chain silently moved
    on. The run then reported "no evidence" while the real cause was a bad
    credential the user could have fixed in ten seconds.
    """

    name = "tavily"
    trusted = True

    def __init__(self) -> None:
        # Single source of truth for "may I make an external request". Callers
        # used to pre-check settings.search_enabled *and* rely on this
        # constructor, so the two gates could disagree and the caller silently
        # ended up with no provider.
        if not settings.search_api_key:
            raise SearchError("no Tavily key configured")
        if not settings.use_live_providers:
            raise SearchError("live providers are disabled")
        self.api_key = settings.search_api_key

    def _post(self, path: str, payload: dict, timeout: float = 25.0) -> dict:
        import json

        body = {**payload, "api_key": self.api_key}
        req = urllib.request.Request(
            settings.search_base_url.rstrip("/") + path,
            data=json.dumps(body).encode("utf-8"),
            headers={**dict(_HEADERS), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if exc.code in (401, 403):
                # A credential problem: waiting or retrying cannot fix it.
                raise SearchError(
                    f"tavily rejected the key (HTTP {exc.code}): {detail}"
                ) from exc
            if exc.code in (429, 432):
                # 432 is Tavily's plan/quota limit for the month.
                raise RateLimitedError(f"tavily quota exhausted (HTTP {exc.code})") from exc
            raise SearchError(f"tavily HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SearchError(f"tavily request failed: {exc}") from exc

    @staticmethod
    def _results(data: dict) -> list[dict]:
        """Tolerate the documented shape and any trivially different one."""
        for key in ("results", "data", "hits", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        return []

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        data = self._post("/search", {
            "query": query,
            "search_depth": "advanced",
            "max_results": max(1, min(limit, 20)),
        })
        rows = self._results(data)
        hits = [
            SearchHit(
                url=sanitize_url(r.get("url") or ""),
                title=(r.get("title") or r.get("url") or "").strip(),
                snippet=(r.get("content") or r.get("snippet") or "")[:1200],
                source=self.name,
            )
            for r in rows
        ]
        hits = [h for h in hits if h.url]
        if not hits:
            # Name the payload keys: a schema change must not look like
            # "this query genuinely has no results".
            raise SearchError(
                f"tavily returned no usable results (keys={sorted(data)[:6]})"
            )
        return hits[:limit]

    def extract(self, url: str) -> tuple[str, str] | None:
        """Fetch a page as clean text. Returns (title, content) or None."""
        try:
            data = self._post("/extract", {"urls": [url]}, timeout=30.0)
        except SearchError:
            return None
        for row in self._results(data):
            text = (row.get("raw_content") or row.get("content") or "").strip()
            if text:
                return (row.get("title") or "").strip(), text
        return None


# ---------------------------------------------------------------------------
# DeepSeek native search (preferred when a DeepSeek key is configured)
# ---------------------------------------------------------------------------


class DeepSeekSearchProvider:
    """DeepSeek's own search engine via the Search API (`/v1/tools/search`).

    Opened to the public on 2026-10-17 and priced per successful request, which
    makes it the only stable option on this network: every free engine either
    throttles by IP (Baidu/360) or serves a fixed degraded result set (Bing).

    Two response fields matter more than the URL list:
    - `summary` is a 600-800 character long abstract. That is long enough to
      contain complete sentences with the actual figures, unlike the 100-200
      character `snippet` that other engines return and that kept getting cut
      mid-sentence.
    - `authority` (0-4) and `rerank_score` (0-3) let the caller rank sources
      instead of treating every page as equally credible.
    """

    name = "deepseek"
    SEARCH_PATH = "/v1/tools/search"
    # Its own engine ranks by relevance/authority, so the heuristic relevance
    # gate would only risk discarding good hits.
    trusted = True

    def __init__(self, api_key: str, base_url: str) -> None:
        if not api_key:
            raise SearchError("no DeepSeek key configured")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict, timeout: float = 30.0) -> dict:
        import json

        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 401/402/429 are account-level states: retrying will not help.
            if exc.code in (401, 402, 429):
                raise RateLimitedError(f"deepseek HTTP {exc.code}") from exc
            raise SearchError(f"deepseek HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SearchError(f"deepseek request failed: {exc}") from exc

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        cache_key = CACHE.make_key("deepseek", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            return [
                SearchHit(url=i["url"], title=i["title"], snippet=i.get("snippet", ""),
                          source=i.get("source", self.name))
                for i in cached if isinstance(i, dict) and i.get("url")
            ]

        data = self._post(self.SEARCH_PATH, {
            "query": query[:128],
            "count": max(1, min(limit, 50)),
            # The long summary is enough to quote from; page bodies are fetched
            # separately so a search result never depends on one big response.
            "need_content": False,
        })

        hits: list[SearchHit] = []
        seen: set[str] = set()
        rows = data.get("results")
        if rows is None:
            # Tolerate a trivially different envelope, but never treat an
            # unrecognised payload as "no results": that is how a schema change
            # silently becomes a zero-evidence run.
            for key in ("data", "hits", "items", "documents"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        if not isinstance(rows, list):
            raise SearchError(
                f"deepseek response has no result list (keys={sorted(data)[:6]})"
            )
        for item in rows:
            if not isinstance(item, dict):
                continue
            url = sanitize_url(item.get("url") or "")
            if not url or url in seen:
                continue
            seen.add(url)
            # Prefer the long summary over the short snippet: it is the field
            # that actually carries complete sentences and figures.
            text = (item.get("summary") or item.get("snippet") or "").strip()
            hits.append(SearchHit(
                url=url,
                title=(item.get("title") or url).strip(),
                snippet=text[:1200],
                source=self.name,
            ))
            if len(hits) >= limit:
                break

        if not hits:
            raise SearchError("deepseek search returned no results")
        CACHE.put(cache_key, [
            {"url": h.url, "title": h.title, "snippet": h.snippet, "source": h.source}
            for h in hits
        ])
        return hits

    def fetch_page(self, url: str) -> tuple[str, str] | None:
        """Fetch one page as Markdown. Returns (title, content) or None."""
        try:
            data = self._post("/v1/tools/fetch", {"url": url})
        except SearchError:
            return None
        content = (data.get("content") or "").strip()
        if not content:
            return None
        return (data.get("title") or "").strip(), content


# ---------------------------------------------------------------------------
# Baidu (most relevant free source for Chinese queries)
# ---------------------------------------------------------------------------


class BaiduSearchProvider:
    """Baidu HTML search.

    Findings from live testing on this machine (2026-11-20):
    - A normal response is ~800 KB of HTML; the anti-bot response is ~2.6 KB,
      so a size floor detects throttling without parsing anything.
    - Every result link is a `/link?url=...` redirect. The real destination is
      only in the redirect target, so it is resolved with one follow-up request
      per returned hit (bounded by `limit`).
    - Snippets are unreliable: much of the page is AI-generated cards
      (`dqaGenCPage`, hashed class names). A best-effort snippet is taken from
      the text following the <h3>, and downstream code must not depend on it —
      the page body is fetched and quoted instead.
    - The page is hydrated with inline JSON, so a naive text run after the <h3>
      ends mid-structure. Measured snippet: `...2028年达8520.35亿元","clamp":2}],
      "isSingleLine":fal`. That punctuation is not part of the sentence, and
      quoting it verbatim put JSON fragments into the report, so the snippet is
      cut at the first structural token.
    """

    name = "baidu"
    ORIGIN = "https://www.baidu.com"
    # Below this size the response is the anti-bot stub, not a result page.
    STUB_LEN = 20_000
    _RESULT_RE = re.compile(
        r'<h3[^>]*>\s*<a[^>]*href="(http://www\.baidu\.com/link\?url=[^"]+|https?://[^"]+)"'
        r'[^>]*>(.*?)</a>',
        re.S,
    )
    # First structural token of the page's inline JSON state.
    _SNIP_CUT_RE = re.compile(r'\\?"\s*[}\]]|\\"|"\s*:\s*[\[{]|"\s*,\s*"')
    # SERP chrome that is never part of an indexed sentence.
    _SNIP_BOILER_RE = re.compile(
        r"查看此网页的中文翻译[，,]?请点击|翻译此页|百度快照|网页快照|"
        r"该网页可能为AI生成[^。]*。?"
    )

    def __init__(self) -> None:
        self.backoff = 0.0

    def _request(self, query: str) -> str:
        url = self.ORIGIN + "/s?" + urllib.parse.urlencode({"wd": query, "rn": "20"})
        return SESSION.get(url, timeout=15.0)

    def _resolve(self, link: str) -> str:
        """Follow a /link?url= redirect to the real destination."""
        try:
            req = urllib.request.Request(link, headers=dict(_HEADERS))
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return sanitize_url(resp.geturl())
        except Exception:
            return ""

    def _clean_snippet(self, raw: str) -> str:
        """Text after the <h3>, stripped of SERP chrome and inline JSON."""
        text = self._SNIP_BOILER_RE.sub("", _strip_tags(raw))
        cut = self._SNIP_CUT_RE.search(text)
        if cut:
            text = text[: cut.start()]
        return re.sub(r"\s+", " ", text).strip()

    def _parse(self, html_text: str, limit: int) -> list[SearchHit]:
        # Each result starts at an <h3>; the text after it is its snippet area.
        chunks = re.split(r"(?=<h3)", html_text)
        out: list[SearchHit] = []
        seen: set[str] = set()
        for chunk in chunks:
            if len(out) >= limit:
                break
            match = self._RESULT_RE.search(chunk)
            if not match:
                continue
            href = _html.unescape(match.group(1))
            title = _strip_tags(match.group(2))
            if len(title) < 5:
                continue
            real = self._resolve(href) if "/link?url=" in href else sanitize_url(href)
            if not real or real in seen:
                continue
            if _INTERNAL_RE.search(urllib.parse.urlsplit(real).netloc):
                continue
            seen.add(real)
            # Snip the text right after the anchor, without the title itself.
            tail = self._clean_snippet(chunk[match.end():])[:260]
            out.append(SearchHit(url=real, title=title, snippet=tail, source=self.name))
        return out

    def search(self, query: str, limit: int = 10, retries: int = 1) -> list[SearchHit]:
        cache_key = CACHE.make_key("baidu", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            return [
                SearchHit(url=i["url"], title=i["title"], snippet=i.get("snippet", ""),
                          source=i.get("source", self.name))
                for i in cached if isinstance(i, dict) and i.get("url")
            ]

        LIMITER.wait(self.ORIGIN, extra=self.backoff)
        try:
            html_text = self._request(query)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SearchError(f"baidu failed: {exc}") from exc

        if len(html_text) < self.STUB_LEN:
            # Retrying a throttled host only extends the throttle.
            raise RateLimitedError("baidu returned an anti-bot stub page")

        hits = self._parse(html_text, limit)
        if not hits:
            raise SearchError("baidu returned no parseable results")

        self.backoff = 0.0
        CACHE.put(cache_key, [
            {"url": h.url, "title": h.title, "snippet": h.snippet, "source": h.source}
            for h in hits
        ])
        return hits


# ---------------------------------------------------------------------------
# Bing web RSS (structured fallback)
# ---------------------------------------------------------------------------


class BingRssProvider:
    """Bing web search through the RSS format.

    Structured output (title/link/description) with no HTML scraping, but the
    ranking is weak for Chinese business queries: unrelated queries can return
    the same generic set. Hits therefore go through the relevance gate in
    `ChainedSearch` before they are used.
    """

    name = "bing-rss"
    ORIGIN = "https://cn.bing.com"

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        cache_key = CACHE.make_key("bing-rss", query, str(limit))
        cached = CACHE.get(cache_key)
        if cached:
            return [
                SearchHit(url=i["url"], title=i["title"], snippet=i.get("snippet", ""),
                          source=i.get("source", self.name))
                for i in cached if isinstance(i, dict) and i.get("url")
            ]

        url = self.ORIGIN + "/search?" + urllib.parse.urlencode(
            {"q": query, "format": "rss", "count": str(limit)}
        )
        LIMITER.wait(self.ORIGIN)
        try:
            body = SESSION.get(url, timeout=15.0)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SearchError(f"bing-rss failed: {exc}") from exc

        head = body.lstrip()[:400].lower()
        if "<rss" not in head and "<?xml" not in head:
            raise SearchError("bing-rss returned HTML instead of a feed")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise SearchError(f"bing-rss bad xml: {exc}") from exc

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = sanitize_url(item.findtext("link") or "")
            if not title or not link or link in seen:
                continue
            if _INTERNAL_RE.search(urllib.parse.urlsplit(link).netloc):
                continue
            desc = _strip_tags(item.findtext("description") or "")
            seen.add(link)
            hits.append(SearchHit(url=link, title=title, snippet=desc[:400], source=self.name))
            if len(hits) >= limit:
                break
        if not hits:
            raise SearchError("bing-rss returned no items")

        CACHE.put(cache_key, [
            {"url": h.url, "title": h.title, "snippet": h.snippet, "source": h.source}
            for h in hits
        ])
        return hits


# Chinese sources often write 智能体 / 人工智能 instead of the Latin term, so each
# term is matched against its synonyms before a hit is judged off-topic.
_TERM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "agent": ("agent", "智能体"),
    "ai": ("ai", "人工智能"),
}


def _term_matches(term: str, blob: str) -> bool:
    return any(v.lower() in blob for v in _TERM_SYNONYMS.get(term.lower(), (term,)))


def _relevance_terms(query: str) -> list[str]:
    """Latin-script tokens that a relevant hit must mention.

    Chinese queries about technology almost always contain a decisive Latin
    token ("AI Agent", "IDC", "SaaS"). Requiring one of them removes the
    generic navigation pages Bing returns for many Chinese business queries,
    while leaving pure-Chinese queries to the Chinese-term gate below.
    """
    terms = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9\.\+]{1,19}", query) if len(t) >= 2]
    return list(dict.fromkeys(terms))


# Research vocabulary that names the *kind* of answer wanted, not the subject.
_GENERIC_CN = (
    "市场规模", "市场份额", "规模", "增长率", "增长", "占比", "渗透率", "出货量",
    "行业", "产业", "分析", "研究", "报告", "现状", "趋势", "发展", "竞争", "格局",
    "商业模式", "技术方向", "技术", "方向", "机会", "风险", "挑战", "背景", "主要",
    "企业", "未来", "深度", "情况", "如何", "以及", "亿元", "万亿", "百分",
)
# Geographic scope modifiers carry no subject either.
_GEO_CN = ("中国", "全国", "国内", "我国", "全球", "世界", "亚太")


def _cn_topic_terms(query: str) -> list[str]:
    """Chinese subject terms left after stripping research vocabulary.

    A pure-Chinese query ("中国智能体 市场规模") produced no Latin terms, so the
    gate was skipped entirely and Bing's degraded "中国政府网 / 中国统计年鉴"
    results were all accepted. This recovers "智能体" as the subject so those
    pages can be rejected.
    """
    text = query
    for word in _GENERIC_CN:
        text = text.replace(word, " ")
    for word in _GEO_CN:
        text = text.replace(word, " ")
    return list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,8}", text)))


# ---------------------------------------------------------------------------
# Engine chain
# ---------------------------------------------------------------------------


def build_engines() -> list[object]:
    """The one place that decides which engines exist and in what order.

    Exposed so /mode can report the real chain instead of a hand-kept copy:
    that copy had already drifted (it listed baidu first and omitted Sogou
    entirely), so the UI advertised an engine set the code did not use.
    """
    engines: list[object] = []
    # DeepSeek's own engine comes first: it is the only one that does not
    # throttle or degrade on this network.
    if settings.deepseek_search_enabled:
        try:
            engines.append(DeepSeekSearchProvider(
                settings.deepseek_api_key, settings.deepseek_base_url))
        except SearchError:
            pass
    if settings.search_enabled:
        try:
            engines.append(TavilyProvider())
        except SearchError:
            pass
    engines.extend([
        # Sogou first among the free engines: measured 2026-11-20 it was the
        # only one that served a request without captcha or degradation, and it
        # returns absolute article URLs. Baidu and 360 stay as independent
        # fallbacks - they throttle on different schedules, so a query one
        # refuses often succeeds on another.
        SogouSearchProvider(),
        BaiduSearchProvider(),
        SoSearchProvider(),
        BingRssProvider(),
        BingNewsProvider(),
    ])
    return engines


def configured_engine_names() -> list[str]:
    """Engine order as the API reports it (no network activity)."""
    return [getattr(e, "name", "?") for e in build_engines()]


class ChainedSearch:
    """Aggregate across engines; succeed if any engine returns results.

    Order matters: DeepSeek's own engine (keyed) comes first because it neither
    throttles nor degrades; then Sogou, Baidu and 360 for Chinese queries; then
    the Bing feeds. One throttled engine no longer means a zero-result run —
    which is what used to happen when 360 was the only relevant engine and it
    hit its rate limit.
    """

    def __init__(self) -> None:
        self.engines: list[object] = build_engines()
        self.last_error = ""
        self.last_engine = ""
        self.errors: list[str] = []
        self._cool_waits = 0
        self._saw_rate_limit = False

    # A throttled engine recovers on its own: measured 2026-11-20, Sogou served
    # a normal 400 KB SERP again roughly 100 s after captcha'ing. Failing the
    # whole sub-question immediately therefore throws away recoverable work, so
    # one bounded pause-and-retry is allowed per query - capped per run so a
    # permanently blocking IP cannot stall the pipeline for minutes.
    # 25s was measured on a hard captcha, but in practice the fallback engine
    # answers within seconds and a 25s stall repeats once per query - two of
    # those back to back add a full minute of dead air. 4s is enough to clear a
    # transient throttle while keeping the pipeline responsive.
    COOL_WAIT_S = 4.0
    MAX_COOL_WAITS = 2

    def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        self.errors = []
        self._saw_rate_limit = False
        hits = self._attempt(query, limit)
        if hits or self._cool_waits >= self.MAX_COOL_WAITS:
            return hits
        if not self._saw_rate_limit:
            return hits
        self._cool_waits += 1
        time.sleep(self.COOL_WAIT_S)
        return self._attempt(query, limit)

    def _attempt(self, query: str, limit: int) -> list[SearchHit]:
        collected: list[SearchHit] = []
        seen: set[str] = set()
        for engine in self.engines:
            try:
                raw = engine.search(query, limit)
            except SearchError as exc:
                self.last_error = str(exc)
                self.errors.append(f"{getattr(engine, 'name', '?')}: {exc}")
                if isinstance(exc, RateLimitedError):
                    self._saw_rate_limit = True
                continue
            # A trusted engine ranks its own results, so the heuristic gate is
            # skipped for it; the free engines still need it.
            hits = raw if getattr(engine, "trusted", False) else self._relevance_gate(query, raw)
            if not hits:
                # Off-topic results are worse than no results: they become
                # "sources" for an unrelated topic. Report and try the next
                # engine instead of accepting them.
                msg = f"{len(raw)} results, none on-topic"
                self.last_error = msg
                self.errors.append(f"{getattr(engine, 'name', '?')}: {msg}")
                continue
            self.last_engine = engine.name
            for hit in hits:
                key = hit.url.split("#")[0].rstrip("/")
                if key not in seen:
                    seen.add(key)
                    collected.append(hit)
            if collected:
                break

        return collected[:limit]

    @staticmethod
    def _relevance_gate(query: str, hits: list[SearchHit]) -> list[SearchHit]:
        """Drop hits that miss the query's subject terms.

        Bing answers many Chinese business queries with generic navigation
        pages ("AI工具集", "节假日安排", "中国政府网", "中国统计年鉴"). Those would
        otherwise be fetched, scored, and printed as findings for an unrelated
        topic — a live run once produced five "AI Agent market" citations that
        were all special treasury bond figures from a government report.

        Two gates run together:
        - Latin terms: most of them must appear ("AI" and "Agent"), accepting
          智能体/人工智能 so Chinese pages are not thrown away.
        - Chinese subject terms: recovered by stripping research vocabulary
          ("市场规模", "竞争格局") and geography from the query, so a pure-Chinese
          query is still gated on its actual subject.

        An empty result means the engine was off-topic; the caller then tries
        the next engine instead of falling back to the garbage.
        """
        if not hits:
            return hits
        latin = _relevance_terms(query)
        chinese = _cn_topic_terms(query)
        if not latin and not chinese:
            return hits

        need_latin = min(2, len(latin))
        kept: list[SearchHit] = []
        for hit in hits:
            blob = (hit.title + " " + hit.snippet).lower()
            if latin and sum(1 for t in latin if _term_matches(t, blob)) < need_latin:
                continue
            if chinese and not any(t in blob for t in chinese):
                continue
            kept.append(hit)
        return kept

    def describe(self) -> str:
        return self.last_engine or ("none (" + "; ".join(self.errors)[:120] + ")")
