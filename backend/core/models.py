"""Core data models for ResearchMind Phase 1.

These dataclasses are the Python mirror of the Pydantic schemas used in
production. Keeping them dependency-free makes the skeleton runnable with
a bare Python 3.10+ install while preserving the exact field contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "spm", "from", "src", "ref",
}


def canonical_url(url: str) -> str:
    """Normalize a URL for dedup: lowercase scheme/host, drop tracking params.

    >>> canonical_url("HTTPS://Example.com/a?utm_source=x&id=7#frag")
    'https://example.com/a?id=7'
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = "&".join(
        f"{k}={v}" for k, v in _parse_query(parts.query)
        if k not in _TRACKING_PARAMS
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))


def _parse_query(query: str) -> list[tuple[str, str]]:
    if not query:
        return []
    out = []
    for item in query.split("&"):
        if "=" in item:
            k, v = item.split("=", 1)
        else:
            k, v = item, ""
        out.append((k, v))
    return out


@dataclass(frozen=True)
class Budget:
    """Hard caps. Every node checks the relevant counter before doing work.

    `max_searches` counts search *calls* (matching the UI label 搜索次数), not
    accumulated results. Counting results instead caused a real defect: a
    7-sub-question plan produced ~17 hits on the first pass, instantly tripped
    the cap, and terminated the loop — so only the first dimension was ever
    researched and every landing evidence was filed under 行业背景.
    `max_hits` caps accumulation separately without forcing termination.
    """

    max_iterations: int = 3
    max_searches: int = 24
    max_hits: int = 80
    max_fetches: int = 40
    max_evidence: int = 120
    max_llm_calls: int = 60
    time_budget_s: float = 600.0

    def exhausted(self, state: "ResearchState") -> bool:
        if state.iteration >= self.max_iterations:
            return True
        if state.search_calls >= self.max_searches:
            return True
        if len(state.documents) >= self.max_fetches:
            return True
        if len(state.evidence) >= self.max_evidence:
            return True
        if state.llm_calls >= self.max_llm_calls:
            return True
        return False


@dataclass
class SubQuestion:
    id: str
    section: str
    question: str
    queries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str
    source: str = "tavily"
    sub_question_id: str = ""
    sub_question_ids: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return canonical_url(self.url)


@dataclass
class Document:
    url: str
    title: str
    raw_text: str
    published_at: str | None = None
    fetch_method: str = "http"
    source_type: str = "secondary"

    @property
    def key(self) -> str:
        return canonical_url(self.url)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Evidence:
    """One atomic claim grounded in one verbatim source span.

    `quote` MUST be a substring of the source document's raw_text. This is
    enforced deterministically in verify.py, not by asking the LLM nicely.
    """

    id: str
    sub_question_id: str
    section: str
    claim: str
    quote: str
    source_url: str
    source_title: str
    published_at: str | None
    source_type: str
    topic: str = ""
    numeric_value: float | None = None
    numeric_unit: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "section": self.section,
            "claim": self.claim,
            "quote": self.quote,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "published_at": self.published_at,
            "source_type": self.source_type,
            "topic": self.topic,
            "numeric_value": self.numeric_value,
            "numeric_unit": self.numeric_unit,
            "confidence": self.confidence,
        }


@dataclass
class Conflict:
    """Two evidence items assert incompatible values for the same topic."""

    topic: str
    unit: str
    left: Evidence
    right: Evidence
    resolution: str = "unresolved"


@dataclass
class ResearchState:
    query: str
    budget: Budget = field(default_factory=Budget)
    plan: list[SubQuestion] = field(default_factory=list)
    search_results: list[SearchHit] = field(default_factory=list)
    documents: list[Document] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    rejected_evidence: list[tuple[str, str]] = field(default_factory=list)
    follow_up_queries: list[str] = field(default_factory=list)
    report: str = ""
    iteration: int = 0
    llm_calls: int = 0
    search_calls: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trace: list[str] = field(default_factory=list)
    on_event: Any = field(default=None, repr=False)
    # Provenance of the run: which engines/sources produced the evidence, and
    # whether this was a real web run or an offline demo. Surfaced in the report
    # so a reader can never mistake fixture data for live research.
    provenance: dict[str, Any] = field(default_factory=dict)

    def log(self, node: str, message: str) -> None:
        entry = f"[{self.iteration}] {node}: {message}"
        self.trace.append(entry)
        if self.on_event is not None:
            self.on_event(node, message, self.iteration)


CITATION_RE = re.compile(r"\[E(\d+)\]")
