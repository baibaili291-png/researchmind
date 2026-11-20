"""Tests for the real-web research path: planner, extractor, URL sanitizing,
rate limiting. These cover the defects found during live integration testing.

Run: RESEARCHMIND_USE_WEB=false python -m unittest discover -s backend/tests -v
"""

from __future__ import annotations

import unittest

from backend.core.models import Document, SubQuestion
from backend.providers.extract import (
    _derive_topic,
    expand_keywords,
    extract_facts,
    metric_subject,
    parse_number,
    score_sentence,
)
from backend.providers.planner import analyze_topic, build_plan, strip_possessive
from backend.providers.readability import extract_main, looks_relevant
from backend.providers import llm as llm_mod
from backend.providers import search as search_mod
from backend.providers.search import sanitize_url


class TestTopicAnalysis(unittest.TestCase):
    """Regression: the subject used to collapse to the bare word "Agent",
    dropping "中国"/"AI" and producing queries that matched nothing useful."""

    def test_subject_keeps_full_noun_phrase(self) -> None:
        analysis = analyze_topic("深度研究2026年中国 AI Agent 行业的市场规模与竞争格局")
        self.assertEqual(analysis.subject, "中国 AI Agent")
        self.assertEqual(analysis.year, "2026")

    def test_instruction_verbs_stripped(self) -> None:
        self.assertEqual(analyze_topic("帮我调研深圳的跨境电商").subject, "深圳跨境电商")

    def test_requested_sections_detected_in_order(self) -> None:
        analysis = analyze_topic("分析国产大模型的商业模式和风险")
        self.assertIn("商业模式", analysis.requested_sections)
        self.assertIn("风险分析", analysis.requested_sections)

    def test_plan_requests_requested_sections_first(self) -> None:
        plan = build_plan("分析国产大模型的商业模式和风险")
        self.assertEqual(plan[0].section, "商业模式")

    def test_plan_queries_are_distinct_and_not_redundant(self) -> None:
        """Regression: queries repeated the subject ("中国 AI Agent 中国 政策")."""
        for sq in build_plan("深度研究2026年中国 AI Agent 行业的市场规模与竞争格局"):
            self.assertTrue(sq.queries)
            for query in sq.queries:
                self.assertEqual(query.count("中国"), 1, f"duplicated subject word in {query!r}")
            self.assertTrue(sq.keywords)

    def test_plan_keywords_deduplicated(self) -> None:
        for sq in build_plan("2026年中国 AI Agent 市场规模"):
            self.assertEqual(len(sq.keywords), len(set(sq.keywords)))


class TestStripPossessive(unittest.TestCase):
    """Regression: a raw 的-stripping regex turned "研究目的与意义" into
    "目与意义". Lexical 的 must survive; only the particle is removed."""

    def test_removes_particle(self) -> None:
        self.assertEqual(strip_possessive("深圳的跨境电商"), "深圳跨境电商")
        self.assertEqual(strip_possessive("中国的AI"), "中国AI")

    def test_protects_lexical_de(self) -> None:
        for word in ("研究目的与意义", "的确如此", "标的物"):
            self.assertEqual(strip_possessive(word), word, msg=word)

    def test_subject_keeps_lexical_de(self) -> None:
        self.assertEqual(analyze_topic("帮我调研深圳的跨境电商").subject, "深圳跨境电商")


class TestUrlSanitizing(unittest.TestCase):
    """Regression: a SERP-relative URL containing raw CJK and a space reached
    urllib and raised InvalidURL, aborting an entire research run."""

    def test_rejects_relative_target(self) -> None:
        self.assertEqual(sanitize_url("/s?q=2026年 agent 发展"), "")

    def test_rejects_empty_and_junk(self) -> None:
        for bad in ("", "javascript:void(0)", "mailto:a@b.c", "ftp://x/y"):
            self.assertEqual(sanitize_url(bad), "", msg=bad)

    def test_encodes_raw_cjk_and_spaces(self) -> None:
        cleaned = sanitize_url("https://example.com/a b?c=中国市场")
        self.assertNotIn(" ", cleaned)
        self.assertNotIn("中", cleaned)
        self.assertTrue(cleaned.startswith("https://example.com/"))

    def test_strips_control_characters(self) -> None:
        cleaned = sanitize_url("https://example.com/a\nb\tc")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\t", cleaned)

    def test_unescapes_entities(self) -> None:
        self.assertEqual(
            sanitize_url("https://example.com/?u=https%3A%2F%2Fx.com&amp;a=1"),
            "https://example.com/?u=https%3A%2F%2Fx.com&a=1",
        )


class TestNumberParsing(unittest.TestCase):
    """Regression: "400,000家" parsed as 0.0 (naive regex) or None (ValueError)."""

    def test_thousands_separator(self) -> None:
        self.assertEqual(parse_number("400,000"), 400000.0)

    def test_thousands_with_decimals(self) -> None:
        self.assertEqual(parse_number("1,234.5"), 1234.5)

    def test_plain_and_decimal(self) -> None:
        self.assertEqual(parse_number("182.34"), 182.34)
        self.assertEqual(parse_number("320"), 320.0)

    def test_invalid_returns_none(self) -> None:
        for bad in (None, "", ",", "abc", "，"):
            self.assertIsNone(parse_number(bad), msg=str(bad))

    def test_regex_captures_full_grouped_number(self) -> None:
        score, numbers = score_sentence("该系统已服务超过400,000家企业客户", ["企业"])
        self.assertIn(("400,000", "家"), numbers)


class TestReadability(unittest.TestCase):
    def test_extracts_article_and_drops_nav(self) -> None:
        html = """
        <html><head><title>测试文章标题</title></head><body>
        <nav><a href="/">首页</a><a href="/login">登录</a><a href="/reg">注册</a></nav>
        <article>
          <p>2026年中国AI Agent市场规模达到320亿元，同比增长68%，其中企业服务占比最高。</p>
          <p>报告指出多智能体协作架构显著提升了复杂任务完成率，工具调用标准化成为关键。</p>
          <p>分析师预计未来三年行业将保持高速增长，垂直场景落地成为主要驱动力之一。</p>
        </article>
        <footer>版权所有 ICP备案 123456</footer>
        </body></html>
        """
        title, body = extract_main(html)
        self.assertEqual(title, "测试文章标题")
        self.assertIn("320亿元", body)
        self.assertNotIn("ICP备案", body)
        self.assertNotIn("登录", body)

    def test_rejects_too_short_body(self) -> None:
        title, body = extract_main("<html><body><p>短。</p></body></html>")
        self.assertEqual(body, "")

    def test_relevance_gate_blocks_offtopic_body(self) -> None:
        """Regression: a JS-gated page yielded only unrelated sports headlines,
        and the extractor promoted them into 'facts'."""
        body = "开拓者送雄鹿12连败垫底。周琦王哲林张镇麟回归。刘德华自曝辅导女儿作业很烦恼。"
        self.assertFalse(looks_relevant(body, ["AI Agent", "市场规模", "智能体"]))

    def test_relevance_gate_passes_ontopic_body(self) -> None:
        body = "2026年中国AI Agent市场规模达到320亿元，智能体渗透率快速提升。"
        self.assertTrue(looks_relevant(body, ["AI Agent", "市场规模", "智能体"]))


class TestFactExtraction(unittest.TestCase):
    def _doc(self, text: str) -> Document:
        return Document(url="https://example.com/a", title="T", raw_text=text,
                        published_at="2026-03-01", source_type="secondary")

    def test_extracts_verbatim_quotes_only(self) -> None:
        text = (
            "2026年中国AI Agent市场规模达到320亿元，同比增长68%，进入规模化商用阶段。"
            "阿里云以21%的份额位居第一，百度智能云占18%。"
        )
        sq = SubQuestion(id="Q2", section="市场分析", question="规模", keywords=["AI Agent", "市场规模"])
        items = extract_facts(self._doc(text), sq, sq.keywords)
        self.assertTrue(items)
        for ev in items:
            self.assertIn(ev.quote, text, "quote must be a verbatim span")
            self.assertTrue(ev.claim)

    def test_boilerplate_with_numbers_is_rejected(self) -> None:
        text = "免责声明：本文数据来源于公开信息，市场规模320亿元，不构成投资建议，转载请注明。"
        sq = SubQuestion(id="Q2", section="市场分析", question="规模", keywords=["市场规模"])
        self.assertEqual(extract_facts(self._doc(text), sq, sq.keywords), [])

    def test_numeric_value_and_unit_captured(self) -> None:
        text = "2026年中国AI Agent市场规模达到320亿元，同比增长68%，行业进入爆发期。"
        sq = SubQuestion(id="Q2", section="市场分析", question="规模", keywords=["市场规模", "AI Agent"])
        items = extract_facts(self._doc(text), sq, sq.keywords)
        values = {(e.numeric_value, e.numeric_unit) for e in items}
        self.assertIn((320.0, "亿元"), values)

    def test_returns_nothing_when_nothing_qualifies(self) -> None:
        """An honest empty result beats padding the report with noise."""
        text = "这篇文章讲了天气很好，大家都很开心的样子，没有什么数据。"
        sq = SubQuestion(id="Q1", section="行业背景", question="背景", keywords=["AI Agent"])
        self.assertEqual(extract_facts(self._doc(text), sq, sq.keywords), [])

    def test_confidence_is_bounded(self) -> None:
        text = "2026年中国AI Agent市场规模达到320亿元，同比增长68%，进入规模化商用阶段。"
        sq = SubQuestion(id="Q2", section="市场分析", question="规模", keywords=["市场规模"])
        for ev in extract_facts(self._doc(text), sq, sq.keywords):
            self.assertGreaterEqual(ev.confidence, 0.0)
            self.assertLessEqual(ev.confidence, 1.0)


class TestTruncatedSnippetFiltering(unittest.TestCase):
    """Regression: SERP snippets are cut mid-word by the engine and carry a
    date footer. One such fragment ("…2026年AI营销Age... 2026年8月6日 -") was
    promoted to evidence E2 and printed in the report as a finding."""

    REAL_SNIPPET = (
        "易观Analysys易观分析:预计2026年中国AI营销Agent市场规模达636亿元(同比增35%),"
        "2030年或超1747亿元.中国AI营销市场规模投入持续加大,2026年AI营销Age... "
        "2026年8月6日 -"
    )

    def test_leading_truncation_marker_keeps_complete_sentences(self) -> None:
        """A snippet that STARTS with "..." must still yield its sentences.

        Regression: the filter once cut the body at the first truncation marker,
        so a snippet whose quoted passage was preceded by "..." produced zero
        sentences and a live run returned an empty report even though the
        snippet contained real figures.
        """
        from backend.providers.extract import split_sentences

        for body in (
            "... 2026年中国AI Agent市场规模达到182.34亿元，同比增长78.03%。行业进入爆发期。",
            "...市场达到182.34亿元，同比增长78.03%。...",
            "2026年中国AI Agent市场规模达到182.34亿元，同比增长78.03%。... 2026年8月6日 -",
        ):
            parts = split_sentences(body)
            self.assertTrue(parts, msg=f"no sentence kept from snippet: {body!r}")
            self.assertTrue(any("182.34" in p for p in parts), msg=str(parts))

    def test_truncated_tail_and_date_footer_are_dropped(self) -> None:
        from backend.providers.extract import split_sentences

        parts = split_sentences(self.REAL_SNIPPET)
        joined = "".join(parts)
        self.assertNotIn("AI营销Age...", joined)
        self.assertNotIn("2026年8月6日", joined)
        # The complete sentence before the truncation must survive.
        self.assertTrue(any("636亿元" in p for p in parts), msg=str(parts))

    def test_every_kept_sentence_ends_with_a_terminator(self) -> None:
        from backend.providers.extract import split_sentences, _TERMINALS

        for part in split_sentences(self.REAL_SNIPPET):
            self.assertIn(part[-1], _TERMINALS, msg=part)

    def test_truncated_fragment_never_becomes_evidence(self) -> None:
        from backend.core.models import Document, SubQuestion
        from backend.providers.extract import extract_facts, _TERMINALS

        doc = Document(url="https://example.org/a", title="T",
                       raw_text=self.REAL_SNIPPET, source_type="secondary")
        sq = SubQuestion(id="Q1", section="市场分析", question="规模",
                         keywords=["AI营销", "市场规模", "Agent"])
        for ev in extract_facts(doc, sq, sq.keywords):
            self.assertNotIn("Age...", ev.quote, "truncated fragment leaked into evidence")
            # Use the same terminal set as the implementation. A half-width "."
            # is a legitimate full stop in SERP snippets ("1747亿元.中国…").
            self.assertIn(ev.quote[-1], _TERMINALS)

    def test_decimal_numbers_survive_terminator_rule(self) -> None:
        from backend.providers.extract import split_sentences

        parts = split_sentences("2025年中国AI Agent市场规模为182.34亿元，同比增长78.03%。")
        self.assertEqual(len(parts), 1)
        self.assertIn("182.34", parts[0])
        self.assertIn("78.03", parts[0])


class TestMetricSubject(unittest.TestCase):
    """Regression: different markets collapsed onto one topic key, so an AI
    marketing figure, an AI digital-human figure and a digital-economy figure
    were reported as three contradictory estimates of the same metric."""

    def test_distinguishes_different_markets(self) -> None:
        cases = {
            "易观分析:预计2026年中国AI营销Agent市场规模达636亿元": "AI营销Agent",
            "36氪获悉,IDC报告显示到2026年中国AI数字人市场规模将达到102.4亿元": "AI数字人",
            "2024年我国数字经济规模稳步提升至59.2万亿元": "数字经济",
        }
        subjects = {metric_subject(s): s for s in cases}
        self.assertEqual(len(subjects), 3, "three distinct markets must yield three topics")
        for sentence, expected in cases.items():
            self.assertEqual(metric_subject(sentence), expected, msg=sentence)

    def test_geo_prefix_normalized(self) -> None:
        """中国AI营销Agent and AI营销Agent must key alike to be comparable."""
        self.assertEqual(
            metric_subject("2026年中国AI营销Agent市场规模达636亿元"),
            metric_subject("2026年AI营销Agent市场规模达636亿元"),
        )

    def test_multiword_latin_subject_not_truncated(self) -> None:
        """Regression: the char class excluded spaces, so "企业级AI Agent规模"
        captured only "Agent", merging enterprise with consumer figures."""
        subject = metric_subject("2025年企业级AI Agent规模预计达232亿元")
        self.assertIn("企业级", subject)
        self.assertIn("Agent", subject)

    def test_topic_keys_are_not_falsely_equal(self) -> None:
        a = _derive_topic("2026年中国AI营销Agent市场规模达636亿元", [("636", "亿元")], ["市场规模"], "市场分析")
        b = _derive_topic("2026年中国AI数字人市场规模达102.4亿元", [("102.4", "亿元")], ["市场规模"], "市场分析")
        c = _derive_topic("2026年中国AI营销Agent市场规模达700亿元", [("700", "亿元")], ["市场规模"], "市场分析")
        self.assertNotEqual(a, b, "different markets must not share a topic key")
        self.assertEqual(a, c, "same market and unit must share a topic key")

    def test_halfwidth_period_does_not_leak_into_subject(self) -> None:
        """Regression: SERP snippets use "." as a full stop. Without splitting
        on it, metric_subject walked back across the sentence boundary and
        returned the garbage topic "或超1747亿元.中国AI营销"."""
        snippet = ("易观分析:预计2026年中国AI营销Agent市场规模达636亿元(同比增35%),"
                   "2030年或超1747亿元.中国AI营销市场规模投入持续加大")
        subject = metric_subject(snippet)
        self.assertEqual(subject, "AI营销Agent")
        self.assertNotIn("1747", subject)
        self.assertNotIn(".", subject)

    def test_decimal_values_survive_sentence_splitting(self) -> None:
        """The half-width period rule must not break decimal numbers."""
        self.assertEqual(metric_subject("2025年中国AI AGENT行业市场规模为182.34亿元"), "AIAGENT")
        from backend.providers.extract import split_sentences
        # Both sentences are above the 18-char minimum, so neither is dropped.
        parts = split_sentences("2025年中国AI Agent市场规模为182.34亿元。行业同比增长78.03%进入爆发期。")
        joined = "".join(parts)
        self.assertIn("182.34", joined)
        self.assertIn("78.03", joined)
        self.assertEqual(len(parts), 2, "one sentence per full-width period")


class TestExpandKeywords(unittest.TestCase):
    def test_multiword_keyword_is_split(self) -> None:
        atoms = expand_keywords(["中国 AI Agent", "市场规模"])
        self.assertIn("AI", atoms)
        self.assertIn("Agent", atoms)
        self.assertIn("市场规模", atoms)

    def test_agent_gains_chinese_synonym(self) -> None:
        self.assertIn("智能体", expand_keywords(["AI Agent"]))

    def test_deduplicates(self) -> None:
        atoms = expand_keywords(["AI Agent", "AI", "Agent"])
        self.assertEqual(len(atoms), len(set(atoms)))


class TestInternalLinkFiltering(unittest.TestCase):
    def test_search_engine_self_links_are_dropped(self) -> None:
        from backend.providers.search import _INTERNAL_RE
        for host in ("www.so.com", "ranks.hao.360.cn", "ai.so.com", "www.sogou.com"):
            self.assertTrue(_INTERNAL_RE.search(host), msg=host)

    def test_article_hosts_are_kept(self) -> None:
        from backend.providers.search import _INTERNAL_RE
        for host in ("www.163.com", "caifuhao.eastmoney.com", "www.jiemian.com"):
            self.assertIsNone(_INTERNAL_RE.search(host), msg=host)


class TestEngineChain(unittest.TestCase):
    """Regression: 360 was the only relevant engine, so once it rate-limited the
    run returned zero results and the report came back empty — with no way to
    tell "no data exists" from "our single engine was throttled"."""

    def _chain(self, engines):
        from backend.providers import search as S
        chain = S.ChainedSearch.__new__(S.ChainedSearch)
        chain.engines = engines
        chain.last_error = ""
        chain.last_engine = ""
        chain.errors = []
        return chain

    def test_throttled_engine_falls_through_to_next(self) -> None:
        from backend.providers import search as S
        from backend.core.models import SearchHit

        class Throttled:
            name = "throttled"

            def search(self, query, limit):
                raise S.RateLimitedError("anti-bot stub")

        class Working:
            name = "working"

            def search(self, query, limit):
                return [SearchHit(url="https://a.example.org/x",
                                  title="2026年AI Agent市场规模达182亿元",
                                  snippet="", source="working")]

        chain = self._chain([Throttled(), Working()])
        hits = chain.search("AI Agent 市场规模", 5)
        self.assertEqual(len(hits), 1, "must fall through, not give up")
        self.assertEqual(chain.last_engine, "working")

    def test_baidu_stub_page_is_rate_limited(self) -> None:
        """An anti-bot stub is ~2.6 KB; treating it as "no results" made the
        engine look merely unproductive instead of throttled."""
        from unittest import mock
        from backend.providers import search as S

        provider = S.BaiduSearchProvider()
        with mock.patch.object(S.LIMITER, "wait", return_value=None), \
             mock.patch.object(S.SESSION, "get", return_value="<html>stub</html>"):
            with self.assertRaises(S.RateLimitedError):
                provider.search("抗反爬检测专用查询", 5)

    def test_bing_rss_items_are_parsed(self) -> None:
        from unittest import mock
        from backend.providers import search as S

        feed = (
            '<?xml version="1.0"?><rss><channel>'
            "<item><title>AI Agent 市场规模达到182亿元</title>"
            "<link>https://a.example.org/x</link>"
            "<description>2026年中国AI Agent市场规模182.34亿元。</description></item>"
            "</channel></rss>"
        )
        with mock.patch.object(S.LIMITER, "wait", return_value=None), \
             mock.patch.object(S.SESSION, "get", return_value=feed):
            hits = S.BingRssProvider().search("AI Agent 市场规模 rss 测试", 5)
        self.assertEqual(len(hits), 1)
        self.assertIn("182.34", hits[0].snippet)

    def test_relevance_gate_drops_generic_pages(self) -> None:
        """Bing answers Chinese business queries with navigation pages; those
        must not become "sources" for an unrelated topic."""
        from backend.providers.search import ChainedSearch
        from backend.core.models import SearchHit

        good = SearchHit(url="https://a.example.org/1", title="2026年AI Agent市场规模达182亿元",
                         snippet="", source="x")
        tools_page = SearchHit(url="https://ai-bot.cn/", title="AI工具集官网",
                               snippet="收录数百个AI工具", source="x")
        holiday = SearchHit(url="https://gov.example.cn/h", title="2026年节假日安排",
                            snippet="元旦春节放假", source="x")

        kept = ChainedSearch._relevance_gate("2026年中国AI Agent市场规模",
                                             [good, tools_page, holiday])
        self.assertIn(good, kept)
        self.assertNotIn(holiday, kept, "page without AI/Agent must be dropped")
        self.assertNotIn(tools_page, kept, "mentioning only 'AI' is not enough for 'AI Agent'")

    def test_offtopic_result_is_rejected_not_accepted(self) -> None:
        """A government work report was once accepted as an "AI Agent market"
        source (five citations, all special treasury bond figures) because the
        gate fell back to the unfiltered list when everything failed."""
        from backend.providers.search import ChainedSearch
        from backend.core.models import SearchHit

        gov = [SearchHit(url="https://gov.example.cn/zhuanti/2026qglh/",
                         title="2026年全国两会_中国政府网",
                         snippet="安排超长期特别国债2500亿元支持消费品以旧换新",
                         source="bing-rss")]
        self.assertEqual(ChainedSearch._relevance_gate("AI Agent 市场规模", gov), [])

    def test_chinese_synonym_passes_gate(self) -> None:
        """Chinese pages say 智能体 instead of "Agent"; those must survive."""
        from backend.providers.search import ChainedSearch
        from backend.core.models import SearchHit

        hit = SearchHit(url="https://a.example.org/1", title="中国智能体市场规模达182亿元",
                        snippet="AI 驱动", source="x")
        self.assertIn(hit, ChainedSearch._relevance_gate("AI Agent 市场规模", [hit]))

    def test_chinese_only_query_is_gated_on_subject(self) -> None:
        """A pure-Chinese query used to bypass the gate completely (it had no
        Latin terms), so Bing's "中国政府网 / 中国统计年鉴" pages were accepted as
        sources about the agent market."""
        from backend.providers.search import ChainedSearch
        from backend.core.models import SearchHit

        gov = SearchHit(url="https://gov.example.cn/a",
                        title="中国政府网_中央人民政府门户网站", snippet="", source="x")
        stats = SearchHit(url="https://stats.example.cn/b",
                          title="中国统计年鉴 - 国家统计局", snippet="", source="x")
        good = SearchHit(url="https://a.example.org/c",
                         title="2026年中国智能体市场规模达182亿元", snippet="", source="x")

        kept = ChainedSearch._relevance_gate("中国智能体 市场规模", [gov, stats, good])
        self.assertIn(good, kept)
        self.assertNotIn(gov, kept)
        self.assertNotIn(stats, kept)

    def test_cn_topic_terms_strip_research_vocabulary(self) -> None:
        from backend.providers.search import _cn_topic_terms

        self.assertEqual(_cn_topic_terms("中国智能体 市场规模"), ["智能体"])
        self.assertEqual(_cn_topic_terms("中国 AI Agent 行业的市场规模与竞争格局"), [])

    def test_chain_skips_offtopic_engine(self) -> None:
        from backend.core.models import SearchHit

        class OffTopic:
            name = "offtopic"

            def search(self, query, limit):
                return [SearchHit(url="https://gov.example.cn/h", title="2026年节假日安排",
                                  snippet="元旦春节放假", source="offtopic")]

        class OnTopic:
            name = "ontopic"

            def search(self, query, limit):
                return [SearchHit(url="https://a.example.org/x",
                                  title="2026年AI Agent市场规模达182亿元",
                                  snippet="", source="ontopic")]

        chain = self._chain([OffTopic(), OnTopic()])
        hits = chain.search("AI Agent 市场规模", 5)
        self.assertEqual([h.source for h in hits], ["ontopic"])


class TestDeepSeekProvider(unittest.TestCase):
    """DeepSeek opened Search/Fetch APIs on 2026-10-17 (api.deepseek.com).
    They are the only search path on this network that neither throttles by IP
    nor serves a fixed degraded result set."""

    def test_long_summary_is_used_as_snippet(self) -> None:
        """The 600-800 char summary carries complete sentences with figures;
        the 100-200 char snippet is what kept getting cut mid-sentence."""
        from unittest import mock
        from backend.providers.search import DeepSeekSearchProvider

        provider = DeepSeekSearchProvider("sk-test", "https://api.deepseek.com")
        payload = {"results": [{
            "title": "2026年中国AI Agent市场规模",
            "url": "https://a.example.org/x",
            "snippet": "短摘要",
            "summary": "2026年中国AI Agent市场规模达182.34亿元，同比增长78.03%。" + "补充说明。" * 20,
        }]}
        with mock.patch.object(provider, "_post", return_value=payload):
            hits = provider.search("AI Agent 市场规模 单元测试专用", 5)

        self.assertEqual(len(hits), 1)
        self.assertIn("182.34", hits[0].snippet)
        self.assertGreater(len(hits[0].snippet), 100, "must prefer the long summary")

    def test_auth_failure_is_rate_limited(self) -> None:
        """401/402/429 are account states: retrying burns time for nothing."""
        from unittest import mock
        from backend.providers import search as S

        provider = S.DeepSeekSearchProvider("sk-bad", "https://api.deepseek.com")
        with mock.patch.object(provider, "_post",
                               side_effect=S.RateLimitedError("deepseek HTTP 401")):
            with self.assertRaises(S.RateLimitedError):
                provider.search("任意查询", 5)

    def test_trusted_engine_result_bypasses_relevance_gate(self) -> None:
        """DeepSeek ranks its own results, so the heuristic gate must not run
        on them — a valid result worded unusually would be wrongly dropped."""
        from unittest import mock
        from backend.providers import search as S
        from backend.core.models import SearchHit

        provider = S.DeepSeekSearchProvider("sk-test", "https://api.deepseek.com")
        self.assertTrue(getattr(provider, "trusted", False))

        chain = S.ChainedSearch.__new__(S.ChainedSearch)
        chain.engines = [provider]
        chain.last_error = ""
        chain.last_engine = ""
        chain.errors = []

        # Title mentions neither "AI" nor "Agent", only 智能体 — the gate would
        # have rejected it, but a trusted engine's ranking is kept.
        hit = SearchHit(url="https://a.example.org/1", title="智能体行业观察",
                        snippet="", source="deepseek")
        with mock.patch.object(provider, "search", return_value=[hit]):
            hits = chain.search("AI Agent 市场规模 门禁跳过测试", 5)
        self.assertEqual([h.url for h in hits], [hit.url])

    def test_fetch_page_returns_markdown(self) -> None:
        from unittest import mock
        from backend.providers.search import DeepSeekSearchProvider

        provider = DeepSeekSearchProvider("sk-test", "https://api.deepseek.com")
        with mock.patch.object(provider, "_post",
                               return_value={"title": "标题", "content": "正文 182.34亿元"}):
            result = provider.fetch_page("https://a.example.org/x")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "标题")
        self.assertIn("182.34", result[1])

    def test_fetch_page_empty_content_returns_none(self) -> None:
        from unittest import mock
        from backend.providers.search import DeepSeekSearchProvider

        provider = DeepSeekSearchProvider("sk-test", "https://api.deepseek.com")
        with mock.patch.object(provider, "_post", return_value={"title": "", "content": ""}):
            self.assertIsNone(provider.fetch_page("https://a.example.org/x"))


class TestDotenvLoading(unittest.TestCase):
    """Regression: `cp deploy/.env.example .env` produced a file nothing read,
    because every setting came from os.getenv. A user's key was silently
    ignored and the failure looked like "DeepSeek didn't work"."""

    def test_pairs_comments_and_quotes(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from backend.config import _load_dotenv

        content = (
            "# a comment\n"
            "\n"
            "RM_TEST_PLAIN=abc\n"
            "RM_TEST_QUOTED=\"quoted value\"\n"
            "RM_TEST_SINGLE='single'\n"
            "RM_TEST_EMPTY=\n"
            "NOT_A_PAIR\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(content, encoding="utf-8")
            for k in ("RM_TEST_PLAIN", "RM_TEST_QUOTED", "RM_TEST_SINGLE", "RM_TEST_EMPTY"):
                os.environ.pop(k, None)
            try:
                _load_dotenv(path)
                self.assertEqual(os.environ["RM_TEST_PLAIN"], "abc")
                self.assertEqual(os.environ["RM_TEST_QUOTED"], "quoted value")
                self.assertEqual(os.environ["RM_TEST_SINGLE"], "single")
                self.assertEqual(os.environ["RM_TEST_EMPTY"], "")
            finally:
                for k in ("RM_TEST_PLAIN", "RM_TEST_QUOTED", "RM_TEST_SINGLE", "RM_TEST_EMPTY"):
                    os.environ.pop(k, None)

    def test_real_env_var_is_not_overridden(self) -> None:
        import os
        import tempfile
        from pathlib import Path
        from backend.config import _load_dotenv

        os.environ["RM_TEST_WIN"] = "from-shell"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text("RM_TEST_WIN=from-file\n", encoding="utf-8")
                _load_dotenv(path)
            self.assertEqual(os.environ["RM_TEST_WIN"], "from-shell")
        finally:
            os.environ.pop("RM_TEST_WIN", None)

    def test_missing_file_is_not_an_error(self) -> None:
        from pathlib import Path
        from backend.config import _load_dotenv

        _load_dotenv(Path("no-such-file-anywhere.env"))


class TestBaiduSnippetCleaning(unittest.TestCase):
    """Baidu hydrates its SERP with inline JSON; the text run after an <h3> ends
    mid-structure. Quoted verbatim, that punctuation goes straight into the
    report as part of a "sentence", so it is cut at the first structural token.
    """

    def test_inline_json_tail_is_cut(self) -> None:
        from backend.providers.search import BaiduSearchProvider

        raw = '2026 年中国人工智能整体市场规模将突破 260 亿美元,AI 智能体作为核心细分领..."}],"clamp":2}],"isSingleLine":fal'
        cleaned = BaiduSearchProvider()._clean_snippet(raw)
        self.assertIn("260 亿美元", cleaned)
        self.assertNotIn("clamp", cleaned)
        self.assertNotIn("isSingleLine", cleaned)
        self.assertNotIn("}]", cleaned)

    def test_translate_prompt_removed(self) -> None:
        from backend.providers.search import BaiduSearchProvider

        cleaned = BaiduSearchProvider()._clean_snippet(
            "查看此网页的中文翻译，请点击\n\n\n翻译此页\n\n市场规模达 636 亿元，同比增长 35%。"
        )
        self.assertNotIn("翻译", cleaned)
        self.assertIn("636 亿元", cleaned)

    def test_clean_text_is_preserved(self) -> None:
        """Must not truncate an ordinary sentence: over-cutting loses the fact."""
        from backend.providers.search import BaiduSearchProvider

        raw = "2023年中国AI Agent市场规模达554亿元,预计2028年达8520.35亿元,年复合增长率72.7%"
        self.assertEqual(BaiduSearchProvider()._clean_snippet(raw), raw)


class TestDeepSeekSearchKeySeparation(unittest.TestCase):
    """The LLM key must not be reused for Search/Fetch unless it *is* a DeepSeek key.

    Measured 2026-11-20: the key supplied for this project belongs to an
    OpenAI-compatible MaaS gateway. It answers /chat/completions with 200 and
    /v1/tools/search with 404. Borrowing it unconditionally put a
    guaranteed-to-fail provider at the head of every engine chain.
    """

    def _key_for(self, env: dict[str, str]) -> str:
        import importlib
        import os
        from unittest import mock

        base = {k: os.environ.get(k, "") for k in
                ("RESEARCHMIND_DEEPSEEK_API_KEY", "RESEARCHMIND_LLM_API_KEY",
                 "RESEARCHMIND_LLM_BASE_URL")}
        with mock.patch.dict(os.environ, {**base, **env}, clear=False):
            mod = importlib.import_module("backend.config")
            return mod._deepseek_search_key()

    def test_gateway_llm_key_is_not_borrowed(self) -> None:
        key = self._key_for({
            "RESEARCHMIND_DEEPSEEK_API_KEY": "",
            "RESEARCHMIND_LLM_API_KEY": "sk-gateway",
            "RESEARCHMIND_LLM_BASE_URL": "https://maas.example.com/compatible-mode/v1",
        })
        self.assertEqual(key, "")

    def test_deepseek_llm_key_is_borrowed(self) -> None:
        key = self._key_for({
            "RESEARCHMIND_DEEPSEEK_API_KEY": "",
            "RESEARCHMIND_LLM_API_KEY": "sk-deepseek",
            "RESEARCHMIND_LLM_BASE_URL": "https://api.deepseek.com/v1",
        })
        self.assertEqual(key, "sk-deepseek")

    def test_explicit_key_always_wins(self) -> None:
        key = self._key_for({
            "RESEARCHMIND_DEEPSEEK_API_KEY": "sk-explicit",
            "RESEARCHMIND_LLM_API_KEY": "sk-gateway",
            "RESEARCHMIND_LLM_BASE_URL": "https://maas.example.com/v1",
        })
        self.assertEqual(key, "sk-explicit")


class TestLlmTruncationEscalation(unittest.TestCase):
    """A reasoning model that burns its budget on reasoning_content returns
    content="" with finish_reason="length". Returning that empty string made a
    whole run look like "no facts found", so the client escalates the budget.
    """

    def _call(self, responses: list[dict]) -> tuple[str, list[int]]:
        import json
        from unittest import mock
        from backend.providers.llm import LLMProvider

        seen: list[int] = []

        class _Resp:
            def __init__(self, payload: dict) -> None:
                self._body = json.dumps(payload).encode()

            def read(self) -> bytes:
                return self._body

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *exc) -> None:
                return None

        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            seen.append(json.loads(req.data)["max_tokens"])
            return _Resp(responses[min(len(seen), len(responses)) - 1])

        provider = LLMProvider(api_key="sk-test", base_url="https://api.example.com/v1",
                               model="deepseek-v4.1-flash")
        with mock.patch.object(llm_mod.urllib.request, "urlopen", fake_urlopen):
            out = provider.chat("sys", "user", max_tokens=1000)
        return out, seen

    def test_empty_reasoning_only_reply_is_retried_with_bigger_budget(self) -> None:
        truncated = {"choices": [{"message": {"content": "", "reasoning_content": "思考中"},
                                  "finish_reason": "length"}]}
        good = {"choices": [{"message": {"content": '{"ok":true}'},
                             "finish_reason": "stop"}]}
        out, budgets = self._call([truncated, good])
        self.assertEqual(out, '{"ok":true}')
        self.assertEqual(budgets[0], 1000)
        self.assertGreater(budgets[1], budgets[0])

    def test_persistent_empty_content_raises_instead_of_silently_returning(self) -> None:
        truncated = {"choices": [{"message": {"content": "", "reasoning_content": "思考中"},
                                  "finish_reason": "length"}]}
        with self.assertRaises(RuntimeError) as ctx:
            self._call([truncated])
        self.assertIn("empty content", str(ctx.exception))


class TestChainCoolWait(unittest.TestCase):
    """A throttled engine recovers on its own (measured ~100 s for Sogou), so a
    fully failed query gets one bounded pause-and-retry instead of ending the
    sub-question with nothing."""

    def _chain(self, engine: object):
        from backend.providers.search import ChainedSearch

        chain = ChainedSearch.__new__(ChainedSearch)
        chain.engines = [engine]
        chain.last_error = ""
        chain.last_engine = ""
        chain.errors = []
        chain._cool_waits = 0
        chain._saw_rate_limit = False
        return chain

    def test_rate_limited_query_is_retried_after_pause(self) -> None:
        from unittest import mock
        from backend.providers.search import RateLimitedError, SearchHit

        class Flaky:
            name = "flaky"
            trusted = True

            def __init__(self) -> None:
                self.calls = 0

            def search(self, query: str, limit: int = 10):
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitedError("flaky served a captcha page")
                return [SearchHit(url="https://a.example.org/x", title="t", snippet="s")]

        engine = Flaky()
        chain = self._chain(engine)
        with mock.patch.object(search_mod.time, "sleep") as sleeper:
            hits = chain.search("中国AI Agent 市场规模", limit=5)

        self.assertEqual(len(hits), 1)
        self.assertEqual(engine.calls, 2)
        sleeper.assert_called_once()
        self.assertEqual(chain._cool_waits, 1)

    def test_wait_budget_is_capped_per_run(self) -> None:
        from unittest import mock
        from backend.providers.search import RateLimitedError

        class AlwaysThrottled:
            name = "blocked"
            trusted = True

            def __init__(self) -> None:
                self.calls = 0

            def search(self, query: str, limit: int = 10):
                self.calls += 1
                raise RateLimitedError("blocked served a captcha page")

        engine = AlwaysThrottled()
        chain = self._chain(engine)
        with mock.patch.object(search_mod.time, "sleep") as sleeper:
            for _ in range(4):
                chain.search("中国AI Agent 市场规模", limit=5)

        # 2 waits allowed, then the chain must fail fast instead of stalling.
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(chain._cool_waits, 2)
        self.assertEqual(engine.calls, 6)  # 4 attempts + 2 retries

    def test_off_topic_only_failure_does_not_pause(self) -> None:
        """A degraded engine returns off-topic results - waiting will not fix that."""
        from unittest import mock

        class OffTopic:
            name = "degraded"
            trusted = False

            def search(self, query: str, limit: int = 10):
                from backend.core.models import SearchHit
                return [SearchHit(url="https://gov.example/z", title="节假日安排", snippet="放假")]

        chain = self._chain(OffTopic())
        with mock.patch.object(search_mod.time, "sleep") as sleeper:
            hits = chain.search("2026年中国AI Agent市场规模", limit=5)
        self.assertEqual(hits, [])
        sleeper.assert_not_called()


class TestTavilyProvider(unittest.TestCase):
    """Tavily is the other paid path. Until now it had no tests at all, and its
    failures all collapsed into one unclassified SearchError - so a rejected key
    looked identical to "this query has no results"."""

    def _provider(self, key: str = "tvly-test"):
        from types import SimpleNamespace
        from unittest import mock
        from backend.providers.search import TavilyProvider

        fake = SimpleNamespace(search_api_key=key,
                               search_base_url="https://api.tavily.com",
                               search_enabled=True, use_live_providers=True)
        patcher = mock.patch.object(search_mod, "settings", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return TavilyProvider()

    def test_results_are_parsed(self) -> None:
        from unittest import mock

        provider = self._provider()
        payload = {"results": [
            {"url": "https://a.example.org/x", "title": "报告A", "content": "市场规模达212亿元"},
            {"url": "https://b.example.org/y", "title": "报告B", "content": "增至449亿元"},
        ]}
        with mock.patch.object(provider, "_post", return_value=payload):
            hits = provider.search("2026中国AI Agent市场规模", 5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].source, "tavily")
        self.assertIn("212", hits[0].snippet)

    def test_alternate_envelope_is_tolerated(self) -> None:
        """A trivially different key name must not be read as "no results"."""
        from unittest import mock

        provider = self._provider()
        payload = {"data": [{"url": "https://c.example.org/z", "title": "T"}]}
        with mock.patch.object(provider, "_post", return_value=payload):
            hits = provider.search("q", 3)
        self.assertEqual(hits[0].url, "https://c.example.org/z")

    def test_unrecognised_payload_names_the_keys(self) -> None:
        from unittest import mock
        from backend.providers.search import SearchError

        provider = self._provider()
        with mock.patch.object(provider, "_post", return_value={"unexpected": []}):
            with self.assertRaises(SearchError) as ctx:
                provider.search("q", 3)
        self.assertIn("keys=", str(ctx.exception))

    def test_rejected_key_is_not_treated_as_throttling(self) -> None:
        """A bad credential must not trigger the chain's cool-wait."""
        import io
        import urllib.error
        from unittest import mock
        from backend.providers.search import RateLimitedError, SearchError

        provider = self._provider()
        err = urllib.error.HTTPError(
            "https://api.tavily.com/search", 401, "Unauthorized", {},
            io.BytesIO(b'{"detail":"invalid api key"}'))
        with mock.patch.object(search_mod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(SearchError) as ctx:
                provider.search("q", 3)
        self.assertNotIsInstance(ctx.exception, RateLimitedError)
        self.assertIn("rejected the key", str(ctx.exception))

    def test_quota_error_is_rate_limited(self) -> None:
        import io
        import urllib.error
        from unittest import mock
        from backend.providers.search import RateLimitedError

        provider = self._provider()
        err = urllib.error.HTTPError(
            "https://api.tavily.com/search", 432, "Plan limit", {}, io.BytesIO(b"{}"))
        with mock.patch.object(search_mod.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RateLimitedError):
                provider.search("q", 3)

    def test_extract_returns_clean_text(self) -> None:
        from unittest import mock

        provider = self._provider()
        payload = {"results": [{"url": "https://a.example.org/x",
                                "raw_content": "2025年中国AI Agent市场规模达182.34亿元。"}]}
        with mock.patch.object(provider, "_post", return_value=payload):
            got = provider.extract("https://a.example.org/x")
        self.assertIsNotNone(got)
        self.assertIn("182.34", got[1])

    def test_extract_failure_returns_none(self) -> None:
        from unittest import mock
        from backend.providers.search import SearchError

        provider = self._provider()
        with mock.patch.object(provider, "_post", side_effect=SearchError("boom")):
            self.assertIsNone(provider.extract("https://a.example.org/x"))


class TestTavilyFetchWiring(unittest.TestCase):
    """Tavily users need a body-fetch fallback, not just snippets.

    Measured on this network: Playwright is an optional install and the reader
    API (r.jina.ai) is blocked by the outbound proxy (tunnel 502), so without
    Tavily Extract every anti-bot page degraded to a SERP snippet.
    """

    def test_fetch_chain_builds_tavily_extractor_from_search_key(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        import backend.providers.fetch as fetch_mod

        fake = SimpleNamespace(
            search_api_key="tvly-test", search_base_url="https://api.tavily.com",
            search_enabled=True, use_live_providers=True,
            deepseek_api_key="", deepseek_base_url="",
            fetch_timeout_s=15.0, min_body_chars=200, playwright_fallback=False,
            reader_api_fallback=False, respect_robots=False, per_domain_delay_s=0.0,
        )
        # Both modules read settings: fetch for its own switches, and the
        # provider (via search) for the key. Patch them to the same object so the
        # test reflects production, where there is only one.
        with mock.patch.object(fetch_mod, "settings", fake), \
                mock.patch.object(search_mod, "settings", fake):
            chain = fetch_mod.FetchChain()
        self.assertIsNotNone(chain._tavily, "Tavily key must enable the extract path")

    def test_no_tavily_extractor_without_key(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        import backend.providers.fetch as fetch_mod

        fake = SimpleNamespace(
            search_api_key="", search_base_url="https://api.tavily.com",
            search_enabled=False, use_live_providers=True,
            deepseek_api_key="", deepseek_base_url="",
            fetch_timeout_s=15.0, min_body_chars=200, playwright_fallback=False,
            reader_api_fallback=False, respect_robots=False, per_domain_delay_s=0.0,
        )
        with mock.patch.object(fetch_mod, "settings", fake), \
                mock.patch.object(search_mod, "settings", fake):
            chain = fetch_mod.FetchChain()
        self.assertIsNone(chain._tavily)


class TestDeepSeekResultEnvelope(unittest.TestCase):
    def _provider(self):
        from backend.providers.search import DeepSeekSearchProvider

        return DeepSeekSearchProvider("sk-test", "https://api.deepseek.com")

    def test_summary_is_preferred_over_snippet(self) -> None:
        from unittest import mock

        provider = self._provider()
        payload = {"results": [{"url": "https://a.example.org/x", "title": "T",
                                "snippet": "短", "summary": "长" * 400}]}
        # Distinct query per test: the response cache is file-backed, so a
        # shared query string let the second test read the first test's result
        # and skip the very call it meant to exercise.
        with mock.patch.object(provider, "_post", return_value=payload):
            hits = provider.search("summary优先-测试专用-1", 3)
        self.assertGreater(len(hits[0].snippet), 300)

    def test_missing_result_list_is_an_error_not_an_empty_result(self) -> None:
        """Otherwise a schema change silently becomes a zero-evidence run."""
        from unittest import mock
        from backend.providers.search import SearchError

        provider = self._provider()
        with mock.patch.object(provider, "_post", return_value={"code": 0, "msg": "ok"}):
            with self.assertRaises(SearchError) as ctx:
                provider.search("信封缺失-测试专用-2", 3)
        self.assertIn("no result list", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
