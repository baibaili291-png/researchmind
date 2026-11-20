"""Adapter interfaces — the seam where real providers plug in.

Phase 1 ships OfflineAdapter so the whole loop runs with zero API keys.
Production swaps each method for Tavily / httpx+trafilatura / DeepSeek while
the graph and verification layers stay untouched.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Protocol

from .models import Document, Evidence, SearchHit, SubQuestion


class ResearchAdapters(Protocol):
    def plan(self, query: str) -> list[SubQuestion]: ...

    def search(self, query: str, sub_question_id: str, limit: int) -> list[SearchHit]: ...

    def fetch(self, hit: SearchHit) -> Document | None: ...

    def extract(self, doc: Document, subq: SubQuestion) -> list[Evidence]: ...

    def write(self, state) -> str: ...


# ---------------------------------------------------------------------------
# Offline adapter: a richer deterministic corpus that exercises the full loop.
# Every `quote` is a verbatim substring of `text` so deterministic verification
# passes. The 320亿 / 285亿 divergence is intentional for conflict detection.
# ---------------------------------------------------------------------------

_FIXTURE_DOCS: dict[str, dict] = {
    "https://caict.example.com/agent-white-paper-2026": {
        "title": "中国信通院：AI Agent 产业发展白皮书（2026）",
        "published": "2026-03-18",
        "type": "primary",
        "text": (
            "中国信通院测算，2026年中国AI Agent核心市场规模约为320亿元，同比增长68%，"
            "主要来自企业级流程自动化与智能客服场景。报告指出，中国AI Agent产业自2023年大模型爆发后进入快速成长期，"
            "2024年至2026年的年均复合增长率超过50%。多智能体协作架构在复杂任务中的成功率较单智能体提升约27个百分点。"
            "从竞争格局看，百度、阿里巴巴、字节跳动与智谱AI构成第一梯队，四家企业合计占据约58%的市场份额。"
            "报告同时强调，当前市场统计口径尚未统一，不同机构的测算存在显著差异，引用时需注明口径来源。"
        ),
    },
    "https://iresearch.example.cn/ai-agent-2026": {
        "title": "艾瑞咨询：2026中国AI Agent行业研究报告",
        "published": "2026-05-09",
        "type": "primary",
        "text": (
            "艾瑞咨询数据显示，2026年中国AI Agent相关市场规模为285亿元，增速约55%。"
            "商业模式以订阅制、按调用量计费和行业解决方案三类为主，其中订阅制占比约42%。"
            "企业采购时最关注部署成本与数据安全两项，分别占受访企业的67%和61%。"
            "竞争格局方面，头部厂商凭借模型能力与渠道优势持续扩大领先，行业集中度CR5达到64%。"
            "艾瑞预计，到2028年企业侧Agent渗透率将超过35%，垂直行业解决方案成为主要增长点。"
        ),
    },
    "https://idc.example.com/tracker-2026": {
        "title": "IDC：中国AI Agent市场跟踪报告（2026H1）",
        "published": "2026-07-22",
        "type": "primary",
        "text": (
            "IDC跟踪数据显示，2026年上半年中国AI Agent相关软件与服务支出达到148亿元，同比增长72%。"
            "从厂商份额看，阿里云以21%位居第一，百度智能云占18%，字节跳动占14%，智谱AI与月之暗面分列其后。"
            "IDC认为，工具调用能力的标准化是Agent规模落地的分水岭，超过60%的新增项目采用标准化工具协议。"
            "在行业分布上，金融、制造与互联网三大行业合计贡献约70%的Agent采购需求。"
        ),
    },
    "https://techcrunch.example.com/agent-stack-2026": {
        "title": "2026 Agent stack: from single agent to orchestration",
        "published": "2026-06-21",
        "type": "secondary",
        "text": (
            "Tool calling and orchestration frameworks have become the standard layer for production agents. "
            "The dominant technical direction in 2026 is multi-agent orchestration with explicit tool-use protocols. "
            "Memory management and long-horizon planning remain the two hardest engineering problems in agent systems. "
            "Evaluation and observability are increasingly treated as first-class infrastructure rather than afterthoughts."
        ),
    },
    "https://gov.example.cn/data-security-agent-2026": {
        "title": "生成式人工智能服务安全管理办法（2026修订）",
        "published": "2026-01-12",
        "type": "primary",
        "text": (
            "办法要求面向境内公众提供服务的生成式AI系统落实数据合规与内容标识义务，"
            "涉及个人信息处理的活动须进行合规评估。监管重点包括数据来源合法性与输出可追溯性。"
            "对于Agent系统自主调用外部工具的行为，办法要求保留完整的调用日志，"
            "并明确责任主体，防范越权操作与幻觉输出带来的合规风险。"
        ),
    },
    "https://vendor.example.com/agent-pricing-2026": {
        "title": "某厂商 Agent 平台定价页",
        "published": "2026-04-30",
        "type": "secondary",
        "text": (
            "企业版按席位订阅，每席位每月 899 元；API 按调用量计费，每千次工具调用 12 元。"
            "私有化部署按项目报价，典型项目起步价约 30 万元。"
            "平台提供标准版与企业版两档，企业版包含私有知识库与审计日志功能。"
        ),
    },
    "https://36kr.example.com/agent-opportunity-2026": {
        "title": "36氪：AI Agent 创业机会与融资观察（2026）",
        "published": "2026-08-15",
        "type": "secondary",
        "text": (
            "2026年上半年，国内AI Agent赛道融资事件超过40起，披露总金额约75亿元，"
            "其中垂直行业Agent与开发工具层最受资本青睐。分析认为，通用Agent同质化严重，"
            "而深耕金融、法律、医疗等垂直场景的解决方案更容易建立壁垒。"
            "未来机会主要集中在三个方向：面向企业的流程自动化、面向个人的智能助理，以及Agent基础设施与评测工具。"
        ),
    },
}

_FIXTURE_HITS: dict[str, list[SearchHit]] = {
    "行业背景": [
        SearchHit("https://caict.example.com/agent-white-paper-2026", "信通院白皮书", "产业背景", source="offline"),
    ],
    "市场规模": [
        SearchHit("https://caict.example.com/agent-white-paper-2026", "信通院白皮书", "320亿元", source="offline"),
        SearchHit("https://iresearch.example.cn/ai-agent-2026", "艾瑞报告", "285亿元", source="offline"),
        SearchHit("https://idc.example.com/tracker-2026", "IDC跟踪", "148亿元", source="offline"),
    ],
    "竞争格局": [
        SearchHit("https://idc.example.com/tracker-2026", "IDC跟踪", "厂商份额", source="offline"),
        SearchHit("https://caict.example.com/agent-white-paper-2026", "信通院白皮书", "第一梯队", source="offline"),
        SearchHit("https://iresearch.example.cn/ai-agent-2026", "艾瑞报告", "CR5", source="offline"),
    ],
    "商业模式": [
        SearchHit("https://iresearch.example.cn/ai-agent-2026", "艾瑞报告", "订阅制42%", source="offline"),
        SearchHit("https://vendor.example.com/agent-pricing-2026", "厂商定价", "899元/席位/月", source="offline"),
    ],
    "技术方向": [
        SearchHit("https://techcrunch.example.com/agent-stack-2026", "Agent stack", "orchestration", source="offline"),
        SearchHit("https://caict.example.com/agent-white-paper-2026", "信通院白皮书", "多智能体协作", source="offline"),
        SearchHit("https://idc.example.com/tracker-2026", "IDC跟踪", "工具调用标准化", source="offline"),
    ],
    "商业机会": [
        SearchHit("https://36kr.example.com/agent-opportunity-2026", "36氪融资观察", "融资机会", source="offline"),
    ],
    "风险监管": [
        SearchHit("https://gov.example.cn/data-security-agent-2026", "管理办法", "数据合规", source="offline"),
    ],
}

_SUBQUESTIONS = [
    SubQuestion(id="Q1", section="行业背景",
                question="中国AI Agent行业是如何发展演进的？",
                queries=["AI Agent 行业背景 发展 演进"]),
    SubQuestion(id="Q2", section="市场分析",
                question="2026年中国AI Agent市场规模有多大？",
                queries=["2026 中国 AI Agent 市场规模 亿元 测算"]),
    SubQuestion(id="Q3", section="竞争格局",
                question="主要企业和市场份额格局如何？",
                queries=["AI Agent 竞争格局 市场份额 厂商"]),
    SubQuestion(id="Q4", section="商业模式",
                question="主流商业模式与定价方式是什么？",
                queries=["AI Agent 商业模式 订阅 按调用量"]),
    SubQuestion(id="Q5", section="技术路线",
                question="技术发展方向有哪些？",
                queries=["AI Agent 技术方向 多智能体 工具调用"]),
    SubQuestion(id="Q6", section="商业机会",
                question="未来有哪些商业机会和融资方向？",
                queries=["AI Agent 创业机会 融资 商业机会"]),
    SubQuestion(id="Q7", section="风险分析",
                question="合规与数据风险有哪些？",
                queries=["生成式AI 数据合规 监管 2026 风险监管"]),
]


def _mk_ev(doc: Document, subq: SubQuestion, claim: str, quote: str, topic: str,
           value: float | None = None, unit: str = "") -> Evidence:
    return Evidence(
        id="", sub_question_id=subq.id, section=subq.section, claim=claim, quote=quote,
        source_url=doc.url, source_title=doc.title, published_at=doc.published_at,
        source_type=doc.source_type, topic=topic, numeric_value=value, numeric_unit=unit,
    )


# (subq id, doc url, claim, quote, topic, value, unit)
_EXTRACTION_RULES: list[tuple[str, str, str, str, str, float | None, str]] = [
    # 行业背景
    ("Q1", "https://caict.example.com/agent-white-paper-2026",
     "中国AI Agent产业自2023年大模型爆发后进入快速成长期",
     "中国AI Agent产业自2023年大模型爆发后进入快速成长期",
     "产业发展阶段", None, ""),
    ("Q1", "https://caict.example.com/agent-white-paper-2026",
     "2024年至2026年的年均复合增长率超过50%",
     "2024年至2026年的年均复合增长率超过50%",
     "产业复合增长率", 50.0, "%"),
    # 市场分析（含冲突）
    ("Q2", "https://caict.example.com/agent-white-paper-2026",
     "2026年中国AI Agent核心市场规模约320亿元，同比增长68%",
     "2026年中国AI Agent核心市场规模约为320亿元，同比增长68%",
     "2026年中国AI Agent市场规模", 320.0, "亿元"),
    ("Q2", "https://iresearch.example.cn/ai-agent-2026",
     "2026年中国AI Agent相关市场规模为285亿元，增速约55%",
     "2026年中国AI Agent相关市场规模为285亿元，增速约55%",
     "2026年中国AI Agent市场规模", 285.0, "亿元"),
    ("Q2", "https://idc.example.com/tracker-2026",
     "2026年上半年中国AI Agent相关软件与服务支出达到148亿元，同比增长72%",
     "2026年上半年中国AI Agent相关软件与服务支出达到148亿元，同比增长72%",
     "2026上半年AI Agent支出", 148.0, "亿元"),
    # 竞争格局
    ("Q3", "https://caict.example.com/agent-white-paper-2026",
     "百度、阿里巴巴、字节跳动与智谱AI构成第一梯队，合计约占58%市场份额",
     "百度、阿里巴巴、字节跳动与智谱AI构成第一梯队，四家企业合计占据约58%的市场份额",
     "第一梯队市场份额", 58.0, "%"),
    ("Q3", "https://idc.example.com/tracker-2026",
     "阿里云以21%份额位居第一，百度智能云18%，字节跳动14%",
     "阿里云以21%位居第一，百度智能云占18%，字节跳动占14%",
     "阿里云市场份额", 21.0, "%"),
    ("Q3", "https://iresearch.example.cn/ai-agent-2026",
     "行业集中度CR5达到64%",
     "行业集中度CR5达到64%",
     "行业集中度CR5", 64.0, "%"),
    # 商业模式
    ("Q4", "https://iresearch.example.cn/ai-agent-2026",
     "商业模式以订阅制、按调用量计费和行业解决方案三类为主，订阅制占比约42%",
     "商业模式以订阅制、按调用量计费和行业解决方案三类为主，其中订阅制占比约42%",
     "订阅制占比", 42.0, "%"),
    ("Q4", "https://vendor.example.com/agent-pricing-2026",
     "某厂商企业版每席位每月899元，API每千次工具调用12元",
     "企业版按席位订阅，每席位每月 899 元；API 按调用量计费，每千次工具调用 12 元",
     "企业版席位月费", 899.0, "元/席位/月"),
    ("Q4", "https://iresearch.example.cn/ai-agent-2026",
     "企业采购时最关注部署成本与数据安全，分别占67%和61%",
     "企业采购时最关注部署成本与数据安全两项，分别占受访企业的67%和61%",
     "采购关注部署成本", 67.0, "%"),
    # 技术路线
    ("Q5", "https://caict.example.com/agent-white-paper-2026",
     "多智能体协作架构在复杂任务中的成功率较单智能体提升约27个百分点",
     "多智能体协作架构在复杂任务中的成功率较单智能体提升约27个百分点",
     "多智能体成功率提升", 27.0, "百分点"),
    ("Q5", "https://techcrunch.example.com/agent-stack-2026",
     "工具调用与编排框架已成为生产级Agent的标准层，2026年主流方向是多智能体编排",
     "Tool calling and orchestration frameworks have become the standard layer for production agents. ",
     "技术路线", None, ""),
    ("Q5", "https://idc.example.com/tracker-2026",
     "超过60%的新增项目采用标准化工具协议",
     "超过60%的新增项目采用标准化工具协议",
     "标准化工具协议采用率", 60.0, "%"),
    ("Q5", "https://techcrunch.example.com/agent-stack-2026",
     "记忆管理与长周期规划仍是Agent系统最难的两个工程问题",
     "Memory management and long-horizon planning remain the two hardest engineering problems in agent systems. ",
     "工程难点", None, ""),
    # 商业机会
    ("Q6", "https://36kr.example.com/agent-opportunity-2026",
     "2026年上半年国内AI Agent赛道融资超过40起，披露金额约75亿元",
     "2026年上半年，国内AI Agent赛道融资事件超过40起，披露总金额约75亿元",
     "赛道融资总额", 75.0, "亿元"),
    ("Q6", "https://36kr.example.com/agent-opportunity-2026",
     "未来机会集中在企业流程自动化、个人智能助理、Agent基础设施与评测工具三个方向",
     "未来机会主要集中在三个方向：面向企业的流程自动化、面向个人的智能助理，以及Agent基础设施与评测工具",
     "未来机会方向", None, ""),
    ("Q6", "https://36kr.example.com/agent-opportunity-2026",
     "垂直行业解决方案更容易建立壁垒",
     "而深耕金融、法律、医疗等垂直场景的解决方案更容易建立壁垒",
     "垂直行业壁垒", None, ""),
    ("Q6", "https://iresearch.example.cn/ai-agent-2026",
     "艾瑞预计到2028年企业侧Agent渗透率将超过35%",
     "艾瑞预计，到2028年企业侧Agent渗透率将超过35%",
     "企业侧渗透率预测", 35.0, "%"),
    # 风险分析
    ("Q7", "https://gov.example.cn/data-security-agent-2026",
     "监管要求落实数据合规与内容标识义务，涉及个人信息处理须进行合规评估",
     "办法要求面向境内公众提供服务的生成式AI系统落实数据合规与内容标识义务，涉及个人信息处理的活动须进行合规评估",
     "监管要求", None, ""),
    ("Q7", "https://gov.example.cn/data-security-agent-2026",
     "Agent自主调用外部工具须保留完整调用日志并明确责任主体",
     "对于Agent系统自主调用外部工具的行为，办法要求保留完整的调用日志，并明确责任主体",
     "工具调用留痕", None, ""),
    ("Q7", "https://caict.example.com/agent-white-paper-2026",
     "市场统计口径尚未统一，不同机构测算存在显著差异",
     "当前市场统计口径尚未统一，不同机构的测算存在显著差异",
     "统计口径风险", None, ""),
]


class OfflineAdapter:
    """Deterministic stand-in for search / fetch / LLM extraction / writing."""

    def __init__(self) -> None:
        self.calls = 0

    def plan(self, query: str) -> list[SubQuestion]:
        self.calls += 1
        return [replace(s, queries=list(s.queries)) for s in _SUBQUESTIONS]

    def search(self, query: str, sub_question_id: str, limit: int) -> list[SearchHit]:
        self.calls += 1
        for key, hits in _FIXTURE_HITS.items():
            if key in query:
                return hits[:limit]
        return []

    def fetch(self, hit: SearchHit, keywords: list[str] | None = None) -> Document | None:
        self.calls += 1
        data = _FIXTURE_DOCS.get(hit.url)
        if not data:
            return None
        return Document(
            url=hit.url, title=data["title"], raw_text=data["text"],
            published_at=data["published"], source_type=data["type"],
            fetch_method="offline",
        )

    def extract(self, doc: Document, subq: SubQuestion) -> list[Evidence]:
        """Simulate LLM structured output with verbatim quotes from the doc."""
        self.calls += 1
        out: list[Evidence] = []
        for sqid, url, claim, quote, topic, value, unit in _EXTRACTION_RULES:
            if sqid == subq.id and url == doc.url:
                out.append(_mk_ev(doc, subq, claim, quote, topic, value, unit))
        return out

    def write(self, state) -> str:
        """Render a structured report whose every factual sentence cites evidence."""
        L: list[str] = []
        L.append("# 2026年中国AI Agent行业深度研究报告")
        L.append("")
        L.append("> 本报告由 ResearchMind 自动生成。所有事实性陈述均引用证据账本条目，")
        L.append("> 数字冲突已在正文中显式披露，每条引用可点击查看原文片段与来源。")
        L.append("")

        sections = ["行业背景", "市场分析", "竞争格局", "技术路线", "商业模式", "商业机会", "风险分析"]
        by_section = {s: [e for e in state.evidence if e.section == s] for s in sections}

        # 执行摘要
        L.append("## 执行摘要")
        def fmt(v: float, unit: str) -> str:
            return f"{int(v) if v == int(v) else v}{unit}"

        mkt = by_section["市场分析"]
        if mkt:
            vals = [fmt(e.numeric_value, e.numeric_unit) for e in mkt if e.numeric_value is not None]
            L.append(f"- 2026年中国AI Agent市场处于高速成长期，多家机构测算规模集中在 {'、'.join(vals[:2])} 区间，"
                     f"增速普遍超过50% [{mkt[0].id}][{mkt[1].id}]。")
        comp = by_section["竞争格局"]
        if comp:
            L.append(f"- 竞争格局初步集中，头部厂商凭借模型与渠道优势占据主要份额 [{comp[0].id}]。")
        tech = by_section["技术路线"]
        if tech:
            L.append(f"- 技术路线从单体Agent转向多智能体编排与标准化工具调用 [{tech[0].id}]。")
        opp = by_section["商业机会"]
        if opp:
            L.append(f"- 资本持续加码，垂直行业解决方案与Agent基础设施被视为核心机会 [{opp[0].id}]。")
        L.append("")

        titles = {
            "行业背景": "行业背景与发展阶段",
            "市场分析": "市场规模与增长",
            "竞争格局": "竞争格局与主要企业",
            "技术路线": "技术发展方向",
            "商业模式": "商业模式与定价",
            "商业机会": "商业机会与资本动向",
            "风险分析": "风险与合规挑战",
        }
        for s in sections:
            L.append(f"## {titles[s]}")
            items = by_section[s]
            if not items:
                L.append("（本轮未收集到可验证证据。）")
            for ev in items:
                src = f"（来源：{ev.source_title}，{ev.published_at or '未标注日期'}）"
                L.append(f"- {ev.claim} [{ev.id}]。{src}")
            L.append("")

        if state.conflicts:
            L.append("## 数据口径差异")
            for c in state.conflicts:
                L.append(f"- 关于「{c.topic}」，不同来源存在口径差异："
                         f"{c.left.numeric_value}{c.unit} [{c.left.id}] 与 "
                         f"{c.right.numeric_value}{c.unit} [{c.right.id}]。")
            L.append("> 上述差异源于统计口径与测算范围不同，报告保留各方原始口径，未做数值合并。")
            L.append("")

        L.append("## 参考资料")
        seen: set[str] = set()
        for ev in state.evidence:
            if ev.source_url not in seen:
                seen.add(ev.source_url)
                L.append(f"- {ev.source_title} — {ev.source_url}")
        L.append("")
        return "\n".join(L)
