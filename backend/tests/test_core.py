"""Regression tests for the deterministic verification layer.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from backend.core.adapters import OfflineAdapter
from backend.core.graph import ResearchGraph
from backend.core.models import (
    Budget, Document, Evidence, ResearchState, SearchHit, SubQuestion, canonical_url,
)
from backend.core.verify import (
    detect_numeric_conflicts,
    evidence_belongs_to_section,
    quote_in_source,
    section_anchors,
    validate_citations,
    verify_evidence,
)


def ev(eid: str, topic: str, value: float, quote: str = "some source text") -> Evidence:
    return Evidence(
        id=eid, sub_question_id="Q1", section="市场分析",
        claim=f"{topic} {value}", quote=quote,
        source_url="https://example.com/a", source_title="Test",
        published_at=None, source_type="secondary",
        topic=topic, numeric_value=value, numeric_unit="亿元",
    )


class TestQuoteVerification(unittest.TestCase):
    def test_verbatim_quote_passes(self) -> None:
        self.assertTrue(quote_in_source("市场规模约为320亿元", "测算，市场规模约为320亿元，同比增长68%。"))

    def test_fabricated_quote_fails(self) -> None:
        self.assertFalse(quote_in_source("市场规模约为999亿元", "测算，市场规模约为320亿元，同比增长68%。"))

    def test_whitespace_and_width_normalization(self) -> None:
        self.assertTrue(quote_in_source("ＡＩ　Ａｇｅｎｔ　市场", "AI Agent 市场报告"))

    def test_too_short_quote_fails(self) -> None:
        self.assertFalse(quote_in_source("增长", "同比增长68%"))


class TestCanonicalUrl(unittest.TestCase):
    def test_tracking_params_removed(self) -> None:
        self.assertEqual(
            canonical_url("HTTPS://WWW.Example.com/a/?utm_source=x&id=7#frag"),
            "https://example.com/a?id=7",
        )


class TestConflictDetection(unittest.TestCase):
    def test_detects_divergent_numbers(self) -> None:
        conflicts = detect_numeric_conflicts([
            ev("E1", "AI Agent市场规模", 320.0),
            ev("E2", "AI Agent市场规模", 285.0),
        ])
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({conflicts[0].left.id, conflicts[0].right.id}, {"E1", "E2"})

    def test_no_conflict_for_similar_numbers(self) -> None:
        conflicts = detect_numeric_conflicts([
            ev("E1", "AI Agent市场规模", 320.0),
            ev("E2", "AI Agent市场规模", 318.0),
        ])
        self.assertEqual(conflicts, [])


class TestCitationAudit(unittest.TestCase):
    def test_passes_when_all_cited(self) -> None:
        items = [ev("E1", "t", 1), ev("E2", "t", 2)]
        audit = validate_citations("结论一 [E1]，结论二 [E2]。", items)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["coverage"], 1.0)

    def test_detects_fabricated_marker(self) -> None:
        audit = validate_citations("结论 [E99]。", [ev("E1", "t", 1)])
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["unresolved"], [99])

    def test_detects_orphan_evidence(self) -> None:
        audit = validate_citations("结论 [E1]。", [ev("E1", "t", 1), ev("E2", "t", 2)])
        self.assertEqual(audit["orphans"], ["E2"])


class TestVerifyEvidence(unittest.TestCase):
    def test_rejects_evidence_with_fabricated_quote(self) -> None:
        state = ResearchState(query="test")
        state.documents = [Document(
            url="https://example.com/a", title="T",
            raw_text="市场规模约为320亿元，同比增长68%。",
        )]
        state.evidence = [ev("E1", "t", 320, "市场规模约为999亿元")]
        verify_evidence(state)
        self.assertEqual(state.evidence, [])
        self.assertEqual(state.rejected_evidence, [("E1", "quote_not_in_source")])


class TestReportSummary(unittest.TestCase):
    """Regression: the executive summary listed figures from three *different*
    markets side by side and called the spread a difference of statistical
    scope. That is a fabricated comparison, not a disclosed conflict."""

    def _state_with(self, items):
        state = ResearchState(query="研究AI Agent市场")
        state.documents = [Document(url="https://example.com/a", title="T", raw_text="x")]
        state.evidence = items
        return state

    def test_distinct_markets_are_not_merged_into_one_metric(self) -> None:
        from backend.core.writer import render_report

        state = self._state_with([
            ev("E1", "AI营销Agent市场规模", 636.0),
            ev("E2", "AI数字人市场规模", 102.4),
        ])
        state.evidence[0].section = "市场分析"
        state.evidence[1].section = "市场分析"
        state.evidence[0].topic = "市场分析:AI营销Agent:亿元"
        state.evidence[1].topic = "市场分析:AI数字人:亿元"
        report = render_report(state)
        self.assertIn("不可直接比较", report)
        self.assertNotIn("各口径差异源于统计范围不同", report)

    def test_same_market_multiple_sources_may_be_listed_together(self) -> None:
        from backend.core.writer import render_report

        state = self._state_with([
            ev("E1", "市场规模", 320.0),
            ev("E2", "市场规模", 285.0),
        ])
        for e in state.evidence:
            e.section = "市场分析"
            e.topic = "市场分析:AI Agent:亿元"
        report = render_report(state)
        self.assertNotIn("不可直接比较", report)
        self.assertIn("320亿元", report)

    def test_nested_summary_bullets_are_not_double_marked(self) -> None:
        """Regression: sub-bullets already carried "- ", and render_summary
        prefixed them again, producing "-     - AI营销Agent：…"."""
        from backend.core.writer import render_report

        state = self._state_with([
            ev("E1", "AI营销Agent", 636.0),
            ev("E2", "AI数字人", 102.4),
        ])
        for e in state.evidence:
            e.section = "市场分析"
        state.evidence[0].topic = "市场分析:AI营销Agent:亿元"
        state.evidence[1].topic = "市场分析:AI数字人:亿元"
        report = render_report(state)
        self.assertNotIn("-     - ", report)
        self.assertNotIn("- - ", report)
        # Each subject still renders as a properly indented sub-bullet.
        self.assertIn("    - AI营销Agent：636亿元 [E1]", report)
        self.assertIn("    - AI数字人：102.4亿元 [E2]", report)

    def test_snippet_sources_are_disclosed(self) -> None:
        from backend.core.writer import render_report

        state = self._state_with([ev("E1", "市场规模", 320.0)])
        state.evidence[0].section = "市场分析"
        state.documents[0].fetch_method = "serp_snippet"
        report = render_report(state)
        self.assertIn("索引摘要", report)
        self.assertIn("结论强度低于完整正文来源", report)

    def test_empty_report_never_claims_quotes_came_from_snippets(self) -> None:
        """With zero evidence there are no quotes, so the snippet disclosure must
        not say quotes were taken from them — that is a self-contradiction."""
        from backend.core.writer import render_report

        state = self._state_with([])
        state.documents[0].fetch_method = "serp_snippet"
        report = render_report(state)
        self.assertNotIn("对应证据的引文取自该摘要原文", report)
        self.assertIn("不含可逐字引用的完整陈述句", report)


class TestProvenance(unittest.TestCase):
    """Regression: a web-mode run whose search engine was throttled silently
    fell back to the bundled example.com fixtures, producing a "real" report
    built entirely from invented numbers."""

    def test_demo_mode_report_is_labelled(self) -> None:
        from backend.core.writer import render_report
        from backend.providers.factory import ResearchPipeline

        state = ResearchState(query="研究测试", provenance={"mode": "demo"})
        report = render_report(state)
        self.assertIn("数据来源说明", report)
        self.assertIn("离线演示模式", report)
        self.assertIn("不代表真实市场事实", report)

    def test_web_mode_reports_throttle_instead_of_inventing(self) -> None:
        from backend.core.writer import render_report

        state = ResearchState(query="研究测试", provenance={
            "mode": "web", "search": "web:failed", "fetch": "web:http-chain",
            "extract": "heuristic", "writer": "deterministic",
            "search_failure": "SearchError: so.com served a captcha page (rate limited)",
            "documents": 0, "evidence": 0, "elapsed_s": 12.3,
        })
        report = render_report(state)
        self.assertIn("检索过程中出现失败", report)
        self.assertIn("rate limited", report)
        self.assertIn("未使用任何编造数据补齐", report)
        self.assertNotIn("离线演示模式", report)
        self.assertIn("未能生成有效结论", report)


class TestNoFixtureFallbackInWebMode(unittest.TestCase):
    """The pipeline must not mix offline fixtures into a web-mode run."""

    def test_search_failure_yields_empty_not_fixtures(self) -> None:
        from backend.providers.factory import ProviderStatus, ResearchPipeline

        class FailingWeb:
            last_search_engine = ""
            fetch_stats = {}

            def search(self, query, sqid, limit):
                raise RuntimeError("so.com served a captcha page (rate limited)")

        pipeline = ResearchPipeline.__new__(ResearchPipeline)
        pipeline.web = FailingWeb()
        pipeline.offline = None
        pipeline.llm = None
        pipeline.demo_mode = False
        pipeline.search_failure = ""
        pipeline.status = ProviderStatus(
            llm="none", search="web:chain", fetch="web:http-chain",
            extract="heuristic", writer="deterministic", mode="web",
        )

        hits = pipeline.search("AI Agent 市场规模", "Q1", 6)
        self.assertEqual(hits, [], "must not fall back to fixture results")
        self.assertIn("captcha", pipeline.search_failure)
        self.assertEqual(pipeline.status.search, "web:failed")

    def test_empty_ledger_never_goes_to_the_llm_writer(self) -> None:
        """Measured: with an empty ledger the LLM writes instructions addressed
        to the researcher ("请补充证据…按下述字段补齐"), so the deliverable became
        a form to fill in rather than a statement that the engines throttled.
        """
        from backend.providers.factory import ProviderStatus, ResearchPipeline

        class FakeLlm:
            def __init__(self) -> None:
                self.called = False

            def write(self, state):  # noqa: ANN001
                self.called = True
                return "请补充证据账本内容"

            def extract(self, doc, subq):  # noqa: ANN001
                return []

        pipeline = ResearchPipeline.__new__(ResearchPipeline)
        pipeline.web = None
        pipeline.offline = None
        pipeline.demo_mode = False
        pipeline.search_failure = ""
        pipeline.llm = FakeLlm()
        pipeline.status = ProviderStatus(
            llm="live:x", search="web:none", fetch="web:http-chain",
            extract="llm", writer="llm", mode="web",
        )

        state = ResearchState(query="2026年中国AI Agent市场规模", budget=Budget())
        out = pipeline.write(state)
        self.assertFalse(pipeline.llm.called, "empty ledger must skip the LLM writer")
        self.assertIn("未能获取到任何可访问来源", out)
        self.assertNotIn("请补充证据", out)

    def test_demo_mode_does_not_construct_web_adapter(self) -> None:
        import os
        from importlib import reload

        import backend.config as config_module
        import backend.providers.factory as factory_module

        os.environ["RESEARCHMIND_USE_WEB"] = "false"
        try:
            reload(config_module)
            reload(factory_module)
            pipeline = factory_module.build_adapter()
            self.assertTrue(pipeline.demo_mode)
            self.assertIsNone(pipeline.web)
            self.assertIsNotNone(pipeline.offline)
            self.assertEqual(pipeline.status.mode, "demo")
        finally:
            os.environ["RESEARCHMIND_USE_WEB"] = "true"
            reload(config_module)
            reload(factory_module)


class TestSectionAnchorGate(unittest.TestCase):
    """A number must substantiate the section it is filed under.

    Live regression (2026-11-20): with only the 市场规模 sub-question executed,
    the ledger held ten rows all labelled 市场规模, five of which were model
    benchmark scores and task success rates. Downstream the writer dutifully
    cited "GPT-6 Astra 在 ExploitBench 达 100%" as market-size evidence.
    """

    @staticmethod
    def _eval(section: str, quote: str, value: float | None,
              unit: str = "%") -> tuple[bool, str]:
        item = Evidence(
            id="E1", sub_question_id="Q1", section=section, claim=quote, quote=quote,
            source_url="https://example.com/a", source_title="T", published_at=None,
            source_type="secondary", topic="t", numeric_value=value,
            numeric_unit=unit, confidence=0.9,
        )
        return evidence_belongs_to_section(item)

    def test_benchmark_score_rejected_from_market_size(self) -> None:
        ok, reason = self._eval(
            "市场规模",
            "国信证券披露，GPT-6 Astra在ExploitBench上达到100%，明显高于 GPT-5.6 Sol的78.5%",
            100.0,
        )
        self.assertFalse(ok)
        self.assertIn("section_mismatch", reason)

    def test_success_rate_rejected_from_market_size(self) -> None:
        ok, _ = self._eval(
            "市场规模", "同一模型、同一提示词，简单方式运行20分钟耗费9美元、成功率42%", 42.0
        )
        self.assertFalse(ok)

    def test_real_market_size_kept(self) -> None:
        ok, _ = self._eval(
            "市场规模",
            "2025年，中国AI AGENT（人工智能体）行业市场规模为182.34亿元，同比增长78.03%",
            182.34,
        )
        self.assertTrue(ok)

    def test_benchmark_score_allowed_under_technical_section(self) -> None:
        ok, _ = self._eval("技术路线", "OSWorld 2.0得分由65.7%升至72.6%", 65.7)
        self.assertTrue(ok)

    def test_prose_evidence_never_gated(self) -> None:
        ok, _ = self._eval("市场规模", "该方案没有披露任何量化指标", None)
        self.assertTrue(ok)

    def test_unknown_section_fails_open(self) -> None:
        # A plan may invent dimension names; gating must not silently empty the ledger.
        ok, _ = self._eval("某种未知维度", "任意数字100%", 100.0)
        self.assertTrue(ok)

    def test_count_unit_rejected_from_market_size(self) -> None:
        """Live regression: a procurement threshold was filed as market size.

        Quote: "(2)服务企业客户数量超过1000家或年营收规模达亿元级" - it carries
        the "营收规模" anchor but the number counts clients, so it is not a
        market measurement.
        """
        ok, reason = self._eval(
            "市场规模", "(2)服务企业客户数量超过1000家或年营收规模达亿元级", 1000.0, "家"
        )
        self.assertFalse(ok)
        self.assertIn("unit_is_count", reason)

    def test_money_unit_still_accepted_in_market_size(self) -> None:
        ok, _ = self._eval("市场规模", "2025年中国AI Agent市场规模达182.34亿元", 182.34)
        self.assertTrue(ok)

    def test_count_unit_allowed_outside_market_sections(self) -> None:
        """Competitor counts and project counts are legitimate elsewhere."""
        ok, _ = self._eval("竞争格局", "据厂商披露，中控技术已落地110个项目", 110.0, "个")
        self.assertTrue(ok)

    def test_section_anchors_resolve_by_substring(self) -> None:
        self.assertIn("规模", section_anchors("市场分析"))
        self.assertIn("份额", section_anchors("竞争格局"))
        self.assertEqual(section_anchors(""), ())


class TestEvidenceFairShare(unittest.TestCase):
    """Every planned dimension must get evidence before any one monopolises it.

    Live regression (2026-11-20): with a flat max_evidence, the first two
    dimensions filled the quota in iteration 1, `Budget.exhausted()` returned
    True immediately, and a 7-dimension plan shipped a report covering 2 of them
    (38 documents fetched, 5 dimensions silently dropped).
    """

    def _state_and_graph(self, n_subq: int, max_evidence: int):
        from backend.providers.factory import ProviderStatus, ResearchPipeline

        plan = [
            SubQuestion(id=f"Q{i}", section=f"维度{i}", question=f"问题{i}", queries=[f"q{i}"])
            for i in range(1, n_subq + 1)
        ]
        docs = [
            Document(url=f"https://example.com/{i}", title=f"文档{i}",
                     raw_text="市场规模达100亿元。" * 10, fetch_method="http",
                     source_type="secondary")
            for i in range(1, n_subq + 1)
        ]

        class Adapter:
            """Yields five citable facts for every (document, sub-question) pair."""

            def extract(self, doc, subq):  # noqa: ANN001
                return [
                    Evidence(
                        id="", sub_question_id=subq.id, section=subq.section,
                        claim=f"{subq.section}事实{k}", quote=f"{doc.title} 市场规模达{100 + k}亿元。",
                        source_url=doc.url, source_title=doc.title, published_at=None,
                        source_type="secondary", topic=f"{subq.section}:规模:亿元",
                        numeric_value=float(100 + k), numeric_unit="亿元", confidence=0.9,
                    )
                    for k in range(5)
                ]

        state = ResearchState(query="q", budget=Budget(max_evidence=max_evidence))
        state.plan = plan
        state.documents = docs
        state.search_results = [
            SearchHit(url=d.url, title=d.title, snippet="") for d in docs
        ]
        for i, hit in enumerate(state.search_results, start=1):
            hit.sub_question_ids.add(f"Q{i}")
            hit.sub_question_id = f"Q{i}"
        return state, ResearchGraph(Adapter(), Budget(max_evidence=max_evidence))

    def test_every_dimension_is_represented(self) -> None:
        state, graph = self._state_and_graph(n_subq=3, max_evidence=9)
        graph.extract(state)

        by_section: dict[str, int] = {}
        for ev in state.evidence:
            by_section[ev.section] = by_section.get(ev.section, 0) + 1
        self.assertEqual(set(by_section), {"维度1", "维度2", "维度3"})
        self.assertEqual(sum(by_section.values()), 9, "quota must be spent, not wasted")
        for section, count in by_section.items():
            self.assertEqual(count, 3, f"{section} got an unfair share")

    def test_fetch_slots_are_interleaved_across_dimensions(self) -> None:
        """Same starvation one stage earlier: 14 queries x 6 hits against a
        40-fetch cap meant the first queries claimed every slot and 4 of 7
        dimensions never received a page."""
        state, graph = self._state_and_graph(n_subq=7, max_evidence=70)
        # Rebuild search results: 6 hits per sub-question, in plan order.
        state.search_results = []
        for i in range(1, 8):
            for k in range(6):
                hit = SearchHit(url=f"https://example.com/{i}/{k}", title=f"d{i}-{k}", snippet="")
                hit.sub_question_ids.add(f"Q{i}")
                state.search_results.append(hit)

        ordered = graph._fair_order(state)
        head = ordered[:7]
        covered = {h.sub_question_ids.copy().pop() for h in head}
        self.assertEqual(len(covered), 7, "first 7 fetch slots must cover all dimensions")

    def test_quota_floor_is_one_per_dimension(self) -> None:
        """A budget smaller than the plan must still not starve later dimensions."""
        state, graph = self._state_and_graph(n_subq=5, max_evidence=3)
        graph.extract(state)
        self.assertGreater(len({e.section for e in state.evidence}), 1)
        self.assertLessEqual(len(state.evidence), 7)


class TestGraphLoop(unittest.TestCase):
    def test_off_section_evidence_never_enters_the_ledger(self) -> None:
        sq = SubQuestion(id="Q1", section="市场规模",
                         question="2026年中国AI Agent市场规模有多大？",
                         queries=["q"])
        doc = Document(url="https://example.com/a", title="AI 月报",
                       raw_text="AI月报。GPT-6 Astra在ExploitBench上达到100%，明显高于GPT-5.6 Sol的78.5%。",
                       fetch_method="http", source_type="secondary")
        state = ResearchState(query="2026年中国AI Agent市场规模", budget=Budget())
        state.plan = [sq]
        state.documents = [doc]
        state.search_results = [SearchHit(url=doc.url, title=doc.title, snippet="")]

        class Adapter(OfflineAdapter):
            def extract(self, doc_: Document, subq: SubQuestion):  # noqa: ANN001
                return [Evidence(
                    id="", sub_question_id=subq.id, section=subq.section,
                    claim="GPT-6 Astra在ExploitBench上达到100%",
                    quote="GPT-6 Astra在ExploitBench上达到100%",
                    source_url=doc_.url, source_title=doc_.title, published_at=None,
                    source_type="secondary", topic="市场规模:GPT6:%",
                    numeric_value=100.0, numeric_unit="%", confidence=0.9,
                )]

        ResearchGraph(Adapter()).extract(state)
        self.assertEqual(state.evidence, [])
        self.assertTrue(any("off-section" in t for t in state.trace))

    def test_end_to_end_citation_audit_passes(self) -> None:
        graph = ResearchGraph(OfflineAdapter(), Budget(max_iterations=3))
        state, audit = graph.run("深度研究2026年中国 AI Agent 行业")
        self.assertGreater(len(state.evidence), 0)
        self.assertTrue(audit["passed"])
        self.assertEqual(audit["unresolved"], [])
        self.assertGreaterEqual(len(state.conflicts), 1)
        self.assertIn("route", " ".join(state.trace))


if __name__ == "__main__":
    unittest.main()
