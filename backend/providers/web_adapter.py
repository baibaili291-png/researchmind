"""Real web research adapter — search, fetch, extract, write, all key-free.

This is the actual working agent: it plans from the user's query, searches the
live web via the multi-engine chain, fetches and extracts readable article
bodies, pulls verbatim numeric facts, and composes a traceable report.

An LLM key upgrades planning/extraction/writing quality but is not required —
the deterministic path below produces a genuine researched report on its own.
"""

from __future__ import annotations

from backend.config import settings
from backend.core.models import Document, Evidence, SearchHit, SubQuestion
from backend.core.writer import render_report
from backend.providers.extract import extract_facts
from backend.providers.fetch import FetchChain, _guess_date, _is_primary
from backend.providers.planner import build_plan
from backend.providers.readability import looks_relevant
from backend.providers.search import ChainedSearch

# Minimum snippet length worth treating as a document in its own right.
_MIN_SNIPPET_CHARS = 60


class WebResearchAdapter:
    def __init__(self) -> None:
        self.search_engine = ChainedSearch()
        self.fetch_chain = FetchChain()
        self.last_search_engine = ""
        self.fetch_stats = self.fetch_chain.stats

    def plan(self, query: str) -> list[SubQuestion]:
        return build_plan(query)

    def search(self, query: str, sub_question_id: str, limit: int) -> list[SearchHit]:
        hits = self.search_engine.search(query, limit=limit)
        self.last_search_engine = self.search_engine.describe()
        for h in hits:
            h.sub_question_id = sub_question_id
        return hits

    def _snippet_document(self, hit: SearchHit, keywords: list[str] | None) -> Document | None:
        """Fall back to the SERP snippet when the page body is unreachable.

        Measured on a live run: 14 candidates yielded only 2 fetchable pages
        (7 too short, 4 off-topic, 1 robots-blocked) while the search snippets
        themselves already carried the reportable numbers. The snippet is text
        the engine indexed *from that page*, so quoting it stays verbatim and
        traceable — and it is labelled `serp_snippet` so the report can tell the
        reader exactly how deep the verification went.
        """
        snippet = (hit.snippet or "").strip()
        if len(snippet) < _MIN_SNIPPET_CHARS:
            return None
        if keywords and not looks_relevant(snippet, keywords, min_hits=1):
            return None
        return Document(
            url=hit.url,
            title=(hit.title or hit.url)[:200],
            raw_text=snippet,
            published_at=_guess_date(snippet, snippet),
            fetch_method="serp_snippet",
            source_type="primary" if _is_primary(hit.url) else "secondary",
        )

    def fetch(self, hit: SearchHit, keywords: list[str] | None = None) -> Document | None:
        doc = self.fetch_chain.fetch(hit, keywords=keywords)
        if doc is None and settings.snippet_fallback:
            doc = self._snippet_document(hit, keywords)
            if doc is not None:
                self.fetch_chain._bump("ok:serp_snippet")
        return doc

    def fetch_many(
        self,
        items: list[tuple[SearchHit, list[str]]],
        max_workers: int = 6,
    ) -> list[Document | None]:
        """Concurrent fetch; per-domain pacing is enforced inside FetchChain.

        Snippet fallback is applied to the survivors here rather than inside
        the pool, so ordering stays aligned with the input items.
        """
        docs = self.fetch_chain.fetch_many(items, max_workers=max_workers)
        if not settings.snippet_fallback:
            return docs
        out: list[Document | None] = []
        for (hit, keywords), doc in zip(items, docs):
            if doc is None:
                doc = self._snippet_document(hit, keywords)
                if doc is not None:
                    self.fetch_chain._bump("ok:serp_snippet")
            out.append(doc)
        return out

    def extract(self, doc: Document, subq: SubQuestion) -> list[Evidence]:
        keywords = list(subq.keywords) or [subq.section]
        return extract_facts(doc, subq, keywords)

    def write(self, state) -> str:
        return render_report(state)


__all__ = ["WebResearchAdapter"]
