"""Cross-process rate limiting and result caching for web providers.

Why this exists (observed, not hypothetical): the key-free search engines
available from this machine rate-limit aggressively. so.com returned a 6 KB
captcha page after roughly a dozen rapid queries, and stayed blocked for
minutes. Sogou behaved the same way. Bing HTML stays reachable but returns
generic navigation results (calendar/holiday pages) for Chinese market-research
queries, so it is not usable as a relevance source here.

Two consequences drive the design:
1. Requests must be paced by a shared limiter, not per-engine counters, or one
   research run burns the whole day's quota on a single host.
2. Successful results must be cached and reused, so a run that hits a captcha
   still completes from cache and reports honestly what it could verify.

Cache is a plain JSON file under the project data dir — no extra service.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token-bucket style limiter keyed by registrable host."""

    def __init__(self, min_interval: float = 1.0, burst: int = 2) -> None:
        self.min_interval = min_interval
        self.burst = burst
        self._last: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()
        self._global_last = 0.0

    @staticmethod
    def host_of(url: str) -> str:
        return urlsplit(url).netloc.lower()

    @staticmethod
    def domain_of(host: str) -> str:
        """Collapse a.b.example.com -> example.com for shared pacing."""
        parts = [p for p in host.split(".") if p]
        if len(parts) <= 2:
            return host
        return ".".join(parts[-2:])

    def wait(self, url: str, extra: float = 0.0) -> float:
        """Block until the request is allowed. Returns seconds waited."""
        domain = self.domain_of(self.host_of(url))
        with self._lock:
            now = time.time()
            since_domain = now - self._last[domain]
            since_global = now - self._global_last
            needed = max(
                self.min_interval - since_domain,
                0.35 - since_global,
                extra,
            )
            if needed > 0:
                time.sleep(needed)
            self._last[domain] = time.time()
            self._global_last = self._last[domain]
            return max(needed, 0.0)

    def penalize(self, url: str, seconds: float) -> None:
        """Back off harder from a host that just rate-limited us."""
        domain = self.domain_of(self.host_of(url))
        with self._lock:
            self._last[domain] = time.time() + max(0.0, seconds - self.min_interval)


LIMITER = RateLimiter()


# ---------------------------------------------------------------------------
# Persistent cache
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    override = os.getenv("RESEARCHMIND_CACHE_DIR")
    if override:
        return Path(override) / "web_cache.json"
    return Path(tempfile.gettempdir()) / "researchmind_web_cache.json"


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    writes: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}


class JsonCache:
    """Tiny JSON-backed cache: {key: {ts, value}}. Thread-safe, atomic writes."""

    def __init__(self, path: Path | None = None, ttl_s: float = 6 * 3600) -> None:
        self.path = path or _cache_path()
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self.stats = CacheStats()
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = raw
        except (json.JSONDecodeError, OSError, ValueError):
            self._data = {}

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def make_key(*parts: str) -> str:
        return "|".join(re.sub(r"\s+", " ", p.strip()) for p in parts)

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if not isinstance(entry, dict):
                self.stats.misses += 1
                return None
            if time.time() - float(entry.get("ts", 0)) > self.ttl_s:
                self.stats.misses += 1
                return None
            self.stats.hits += 1
            return entry.get("value")

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {"ts": time.time(), "value": value}
            self.stats.writes += 1
            self._flush()

    def size(self) -> int:
        return len(self._data)


CACHE = JsonCache()
