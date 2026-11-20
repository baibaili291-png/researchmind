"""Provider factory — chooses the best available research pipeline.

Two mutually exclusive modes, decided by RESEARCHMIND_USE_WEB:

* Web mode (default ON): every source is a real page from the live web. When a
  search engine throttles us or a page cannot be fetched, that sub-question
  simply yields no evidence and the report states the gap. Fabricated fixtures
  are NEVER mixed into a web-mode report.
* Demo mode (RESEARCHMIND_USE_WEB=false): the bundled OfflineAdapter fixtures
  are used, so the loop can be exercised and unit-tested with no network.
  Reports from this mode are labelled as a demo.

Why the hard separation: an earlier version silently fell back to the offline
fixtures whenever live search failed. A run against a rate-limited engine then
produced a "real" report built entirely from hand-written example.com numbers.
A research agent that invents sources when it cannot reach them is worse than
one that reports an empty result, so the fallback was removed rather than
tuned.

An LLM key upgrades planning / extraction / writing in either mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.config import settings
from backend.core.models import Document, Evidence, SearchHit, SubQuestion


@dataclass
class ProviderStatus:
    llm: str
    search: str
    fetch: str
    extract: str
    writer: str
    mode: str

    def as_dict(self) -> dict[str, str]:
        return {
            "llm": self.llm, "search": self.search, "fetch": self.fetch,
            "extract": self.extract, "writer": self.writer, "mode": self.mode,
        }


class ResearchPipeline:
    """Implements the ResearchAdapters protocol over real web sources."""

    def __init__(self) -> None:
        self.web = None
        self.offline = None
        self.llm = None
        self.demo_mode = not settings.use_web
        self.search_failure = ""

        self.status = ProviderStatus(
            llm="none",
            search="web:chain" if not self.demo_mode else "offline-fixture",
            fetch="web:http-chain" if not self.demo_mode else "offline-fixture",
            extract="heuristic" if not self.demo_mode else "offline-fixture",
            writer="deterministic",
            mode="demo" if self.demo_mode else "web",
        )

        if self.demo_mode:
            from backend.core.adapters import OfflineAdapter

            self.offline = OfflineAdapter()
        else:
            try:
                from backend.providers.web_adapter import WebResearchAdapter

                self.web = WebResearchAdapter()
                self.status.search = "web:" + (self.web.search_engine.describe() or "chain")
            except Exception as exc:  # pragma: no cover - defensive
                self.web = None
                self.search_failure = f"{type(exc).__name__}: {exc}"
                self.status.search = f"unavailable ({type(exc).__name__})"

        if settings.llm_enabled:
            try:
                from backend.providers.llm import LLMProvider

                self.llm = LLMProvider()
                self.status.llm = f"live:{settings.llm_model}"
                self.status.extract = "llm"
                self.status.writer = "llm"
            except Exception:
                self.llm = None

    # -- protocol ------------------------------------------------------------
    def plan(self, query: str) -> list[SubQuestion]:
        if self.llm:
            try:
                result = self.llm.plan(query)
                if result:
                    return result
            except Exception:
                pass
        if self.web:
            return self.web.plan(query)
        return self.offline.plan(query)

    def search(self, query: str, sub_question_id: str, limit: int) -> list[SearchHit]:
        if self.web:
            try:
                hits = self.web.search(query, sub_question_id, limit)
            except Exception as exc:
                # No fixture fallback: report the failure and yield nothing.
                self.search_failure = f"{type(exc).__name__}: {exc}"
                self.status.search = "web:failed"
                return []
            if hits:
                self.status.search = "web:" + self.web.last_search_engine
                self.search_failure = ""
            return hits
        return self.offline.search(query, sub_question_id, limit)

    def fetch(self, hit: SearchHit, keywords: list[str] | None = None) -> Document | None:
        if self.web:
            return self.web.fetch(hit, keywords=keywords)
        return self.offline.fetch(hit)

    def fetch_many(self, items: list[tuple[SearchHit, list[str]]]) -> list[Document | None]:
        """Concurrent batch fetch. Per-domain pacing lives inside FetchChain."""
        if self.web:
            try:
                return self.web.fetch_many(items)
            except Exception:
                return [self.web.fetch(h, keywords=kw) for h, kw in items]
        return [self.offline.fetch(h) for h, _ in items]

    @property
    def fetch_stats(self) -> dict:
        if self.web:
            return dict(self.web.fetch_stats)
        return {}

    def extract(self, doc: Document, subq: SubQuestion) -> list[Evidence]:
        if self.llm and doc.fetch_method != "offline":
            try:
                result = self.llm.extract(doc, subq)
                if result:
                    return result
            except Exception:
                pass
        if self.web:
            return self.web.extract(doc, subq)
        return self.offline.extract(doc, subq)

    def write(self, state) -> str:
        # An empty ledger must not go to the LLM writer. Measured: it responds
        # with instructions addressed to the researcher ("请补充证据…按下述字段
        # 补齐"), i.e. the system prompt leaks into the deliverable, and the user
        # is told to go fill in a table instead of being told the engines were
        # throttled. The deterministic renderer derives the real cause from the
        # run's own fetch statistics, so it is both truthful and actionable.
        if self.llm and getattr(state, "evidence", None):
            try:
                return self.llm.write(state)
            except Exception:
                pass
        from backend.core.writer import render_report

        return render_report(state)

    def describe(self) -> dict[str, str]:
        """Human-readable provenance, surfaced in the report and the API."""
        info = self.status.as_dict()
        if self.search_failure:
            info["search_failure"] = self.search_failure
        return info


def build_adapter() -> ResearchPipeline:
    return ResearchPipeline()
