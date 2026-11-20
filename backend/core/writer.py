"""Deterministic report writer.

Composes a readable research report strictly from the evidence ledger:
- every factual sentence carries an [E#] marker resolving to a ledger row
- numeric disagreements across sources are disclosed, never averaged away
- sections with no verified evidence say so explicitly instead of being padded
- gaps in coverage are reported as an honest limitation

An LLM writer may be used when a key is configured; this module is the floor
that guarantees the report is traceable even with zero external services.
"""

from __future__ import annotations

from collections import defaultdict

from backend.core.models import Conflict, Evidence, ResearchState

SECTION_ORDER = [
    "行业背景", "市场分析", "竞争格局", "技术路线",
    "商业模式", "商业机会", "风险分析",
]
SECTION_TITLES = {
    "行业背景": "行业背景与发展阶段",
    "市场分析": "市场规模与增长",
    "竞争格局": "竞争格局与主要企业",
    "技术路线": "技术发展方向",
    "商业模式": "商业模式与定价",
    "商业机会": "商业机会与资本动向",
    "风险分析": "风险与合规挑战",
}


def _fmt_num(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    shown = f"{value:g}"
    return f"{shown}{unit}"


def _fmt_date(value: str | None) -> str:
    return value or "未标注日期"


def _group(evidence: list[Evidence]) -> dict[str, list[Evidence]]:
    grouped: dict[str, list[Evidence]] = defaultdict(list)
    for ev in evidence:
        grouped[ev.section].append(ev)
    return grouped


def _ordered_sections(grouped: dict[str, list[Evidence]]) -> list[str]:
    known = [s for s in SECTION_ORDER if s in grouped]
    extra = [s for s in grouped if s not in SECTION_ORDER]
    return known + sorted(extra)


def _topic_subject(topic: str) -> str:
    """Pull the measured subject out of a topic key (section:subject:unit)."""
    parts = (topic or "").split(":")
    return parts[1] if len(parts) > 1 and parts[1] else "相关指标"


def render_summary(grouped: dict[str, list[Evidence]],
                   conflicts: list[Conflict],
                   doc_count: int) -> list[str]:
    """Write the executive summary from the strongest verified findings."""
    lines = ["## 执行摘要", ""]
    bullets: list[str] = []

    market = grouped.get("市场分析", [])
    numeric = [e for e in market if e.numeric_value is not None]
    if numeric:
        # Group by the *measured subject*, not just by section. A previous
        # version listed "636亿元、102.4亿元、182.34亿元" together and called the
        # spread a difference of statistical scope — but those were three
        # different markets (AI营销Agent / AI数字人 / AI Agent整体). Presenting
        # them as one metric is a fabricated comparison, so each subject is now
        # stated on its own line with its own citation.
        by_subject: dict[str, list[Evidence]] = defaultdict(list)
        for ev in numeric:
            by_subject[_topic_subject(ev.topic)].append(ev)
        if len(by_subject) == 1:
            subject, items = next(iter(by_subject.items()))
            values = "、".join(_fmt_num(e.numeric_value, e.numeric_unit) for e in items[:3])
            refs = "".join(f"[{e.id}]" for e in items[:3])
            bullets.append(f"市场规模方面，{subject}的已核实测算为 {values}{refs}。")
        else:
            bullets.append(
                f"市场规模方面，各来源统计的是不同细分市场，不可直接比较，分别列示如下："
            )
            for subject, items in list(by_subject.items())[:4]:
                ev = max(items, key=lambda e: e.confidence)
                bullets.append(
                    f"    - {subject}：{_fmt_num(ev.numeric_value, ev.numeric_unit)} "
                    f"[{ev.id}]（{ev.source_title}）"
                )
    else:
        bullets.append("本轮未从可访问来源中获取到可核实的市场规模数据。")

    comp = grouped.get("竞争格局", [])
    if comp:
        first = comp[0]
        bullets.append(f"竞争格局方面，{first.claim} [{first.id}]。")

    tech = grouped.get("技术路线", [])
    if tech:
        bullets.append(f"技术路线方面，{tech[0].claim} [{tech[0].id}]。")

    opp = grouped.get("商业机会", [])
    if opp:
        bullets.append(f"机会与资本方面，{opp[0].claim} [{opp[0].id}]。")

    risk = grouped.get("风险分析", [])
    if risk:
        bullets.append(f"风险方面，{risk[0].claim} [{risk[0].id}]。")

    if conflicts:
        c = conflicts[0]
        bullets.append(
            f"数据可信度方面，「{c.topic}」存在 {_fmt_num(c.left.numeric_value, c.unit)} "
            f"[{c.left.id}] 与 {_fmt_num(c.right.numeric_value, c.unit)} [{c.right.id}] 的口径差异，"
            "报告保留双方原始数据，未做合并或取舍。"
        )

    bullets.append(
        f"本次研究共抓取 {doc_count} 个可访问来源，产出 {sum(len(v) for v in grouped.values())} "
        "条经逐字引文校验的证据，全部结论可追溯至原始出处。"
    )

    for b in bullets:
        # Nested sub-bullets already carry their own marker and indentation;
        # prefixing them again produced "-     - AI营销Agent：…", which rendered
        # as a malformed double list item.
        if b.lstrip().startswith("- "):
            lines.append(b)
        else:
            lines.append(f"- {b}")
    lines.append("")
    return lines


def render_body(grouped: dict[str, list[Evidence]]) -> list[str]:
    lines: list[str] = []
    for section in _ordered_sections(grouped):
        items = grouped[section]
        title = SECTION_TITLES.get(section, section)
        lines.append(f"## {title}")
        lines.append("")
        # Group by topic so related numbers read together.
        by_topic: dict[str, list[Evidence]] = defaultdict(list)
        for ev in items:
            by_topic[ev.topic or ev.section].append(ev)
        for topic, group in by_topic.items():
            for ev in group[:4]:
                value = _fmt_num(ev.numeric_value, ev.numeric_unit)
                suffix = f"（{value}）" if value else ""
                lines.append(
                    f"- {ev.claim}{suffix} [{ev.id}]"
                    f"（来源：{ev.source_title}，{_fmt_date(ev.published_at)}，"
                    f"{'一手' if ev.source_type == 'primary' else '二手'}来源）"
                )
        lines.append("")
    return lines


def render_conflicts(conflicts: list[Conflict]) -> list[str]:
    if not conflicts:
        return []
    lines = ["## 数据口径差异", ""]
    for c in conflicts:
        lines.append(
            f"- 「{c.topic}」：{_fmt_num(c.left.numeric_value, c.unit)} [{c.left.id}]"
            f"（{c.left.source_title}） 对比 "
            f"{_fmt_num(c.right.numeric_value, c.unit)} [{c.right.id}]"
            f"（{c.right.source_title}）。"
        )
    lines.append("")
    lines.append("> 差异源于统计口径与测算范围不同。本报告并列呈现双方原始数据，")
    lines.append("> 引用时请指明具体机构，避免跨口径直接比较。")
    lines.append("")
    return lines


def render_gaps(state: ResearchState) -> list[str]:
    """Report what could NOT be verified — as informative as what could."""
    covered = {ev.section for ev in state.evidence}
    missing = [SECTION_TITLES.get(s, s) for s in SECTION_ORDER if s not in covered]
    rejected = len(state.rejected_evidence)
    lines = ["## 研究局限与未覆盖项", ""]
    if missing:
        lines.append(f"- 以下维度未获得通过校验的证据：{'、'.join(missing)}。")
    if rejected:
        reasons: dict[str, int] = defaultdict(int)
        for _, reason in state.rejected_evidence:
            reasons[reason] += 1
        detail = "，".join(f"{k} {v} 条" for k, v in reasons.items())
        lines.append(f"- 共有 {rejected} 条候选证据在引文校验阶段被拒绝（{detail}），未写入报告。")
    docs = len(state.documents)
    lines.append(f"- 本次实际纳入分析的可访问来源 {docs} 个；抓取失败或与主题无关的页面已自动跳过。")

    snippet_docs = [d for d in state.documents if d.fetch_method == "serp_snippet"]
    # Only claim quotes were taken from snippets when evidence actually came from
    # them. With zero evidence the old wording said "对应证据的引文取自该摘要原文",
    # which is a self-contradiction in an empty report.
    snippet_urls = {d.url for d in snippet_docs}
    snippet_evidence = [e for e in state.evidence if e.source_url in snippet_urls]
    if snippet_evidence:
        lines.append(
            f"- 其中 {len(snippet_docs)} 个来源仅获取到搜索引擎索引摘要（页面需 JS 渲染或被 robots 限制），"
            f"对应 {len(snippet_evidence)} 条证据的引文取自该摘要原文，结论强度低于完整正文来源。"
        )
    lines.append("- 所有数值均引自来源原文，未做二次推算；如需跨机构可比数据，建议补充一手报告原文。")
    lines.append("")
    return lines


def _explain_empty(state: ResearchState) -> list[str]:
    """Explain, from the run's own data, why no evidence survived validation.

    The earlier wording was generic ("sources were rate limited, pages need JS").
    On a real run the actual cause was different and much more specific: every
    snippet was cut mid-sentence ("…规划纲要》提..."), so no complete quotable
    statement existed. Naming the real cause is the difference between a report
    the reader can act on and one that only says "it failed".
    """
    docs = state.documents
    if not docs:
        return [
            "未能获取到任何可访问来源（检索阶段失败或结果为空）。",
            "常见原因：检索引擎限流或返回验证码、网络不可达、查询主题过于生僻。",
        ]
    snippet_docs = [d for d in docs if d.fetch_method == "serp_snippet"]
    full_docs = [d for d in docs if d.fetch_method != "serp_snippet"]
    if snippet_docs and not full_docs:
        return [
            f"共检索到 {len(docs)} 个来源，但全部无法抓取正文（页面需 JS 渲染或被反爬限制），"
            "只获得搜索引擎索引摘要。",
            "这些摘要在截断处结束（例如“…规划纲要》提”），不含可逐字引用的完整陈述句，"
            "把半句话当证据会构成事实性误导，因此不予采用。",
            "改进方向：为该站点启用可执行 JS 的抓取，或接入可返回正文摘要的搜索 API。",
        ]
    if snippet_docs:
        return [
            f"共 {len(docs)} 个来源（{len(full_docs)} 个正文、{len(snippet_docs)} 个索引摘要），"
            "均未包含通过校验的完整陈述句。",
            "常见原因：正文过短或被截断、内容与查询主题无关。",
        ]
    return [
        f"共抓取 {len(docs)} 个来源正文，但均未包含通过校验的完整陈述句。",
        "常见原因：正文过短、正文被截断，或与查询主题无关。",
    ]


def render_sources(state: ResearchState) -> list[str]:
    lines = ["## 参考资料", ""]
    seen: dict[str, tuple[str, str | None, str, str]] = {}
    for doc in state.documents:
        seen.setdefault(doc.url, (doc.title, doc.published_at, doc.source_type, doc.fetch_method))
    for url, (title, date, stype, method) in seen.items():
        tag = "一手来源" if stype == "primary" else "二手来源"
        depth = "索引摘要" if method == "serp_snippet" else "完整正文"
        lines.append(f"- [{title}]({url}) — {_fmt_date(date)}，{tag}，{depth}")
    lines.append("")
    return lines


def render_provenance(state: ResearchState) -> list[str]:
    """Disclose where the evidence came from.

    A demo-mode report built from bundled fixtures must never look like live
    research, and a web run whose engine was throttled mid-way must say so.
    """
    prov = state.provenance or {}
    mode = prov.get("mode", "")
    lines = ["## 数据来源说明", ""]
    if mode == "demo":
        lines.append("- **本报告运行在离线演示模式**：来源为项目内置的示例资料（`*.example.com`），")
        lines.append("  数据仅用于演示流程与校验逻辑，不代表真实市场事实。")
        lines.append("- 设置 `RESEARCHMIND_USE_WEB=true` 并重启服务即可执行真实联网研究。")
    else:
        engine = prov.get("search", "unknown")
        lines.append(f"- 检索引擎：{engine}；抓取方式：{prov.get('fetch', 'unknown')}。")
        lines.append(f"- 证据抽取：{prov.get('extract', 'unknown')}；报告撰写：{prov.get('writer', 'deterministic')}。")
        if prov.get("search_failure"):
            lines.append(f"- **检索过程中出现失败**：{prov['search_failure']}。")
            lines.append("  受影响维度未获得证据，已在「研究局限与未覆盖项」中列明，系统未使用任何编造数据补齐。")
    if prov.get("elapsed_s") is not None:
        lines.append(
            f"- 本次运行：{prov.get('documents', 0)} 个来源、{prov.get('evidence', 0)} 条证据，"
            f"耗时 {prov['elapsed_s']} 秒。"
        )
    lines.append("")
    return lines


def render_report(state: ResearchState) -> str:
    grouped = _group(state.evidence)
    parts: list[str] = []
    parts.append(f"# {state.query}")
    parts.append("")
    parts.append("> 本报告由 ResearchMind 自动生成。所有事实性陈述均带 [E#] 引用标记，")
    parts.append("> 可点击核对逐字引文与原始来源；数据口径冲突已在正文中显式披露。")
    parts.append("")
    parts.extend(render_provenance(state))
    if grouped:
        parts.extend(render_summary(grouped, state.conflicts, len(state.documents)))
        parts.extend(render_body(grouped))
        parts.extend(render_conflicts(state.conflicts))
        parts.extend(render_gaps(state))
        parts.extend(render_sources(state))
    else:
        parts.append("## 未能生成有效结论")
        parts.append("")
        parts.extend(render_gaps(state))
        parts.append("本次研究未能从可访问来源中提取到通过校验的证据，因此不输出任何结论。")
        parts.extend(_explain_empty(state))
        parts.append("")
    return "\n".join(parts)


__all__ = ["render_report", "SECTION_ORDER", "SECTION_TITLES"]
