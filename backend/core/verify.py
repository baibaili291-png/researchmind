"""Deterministic verification — no LLM judgment involved.

This module is the answer to "阶段三才做 Citation 验证" being too late.
Every check here is a pure function over the evidence ledger, so it can run
on every iteration from day one and its result is reproducible.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Iterable

from .models import CITATION_RE, Conflict, Document, Evidence, ResearchState


_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[\u3000\s,，。.;；:：!！?？、·\-—()（）\[\]【】\"'“”‘’]")


def normalize_text(text: str) -> str:
    """NFKC + collapse whitespace. Used for quote matching only."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def quote_in_source(quote: str, raw_text: str) -> bool:
    """Check that an extracted quote actually appears in the source.

    Whitespace and full/half-width differences are normalized, but no
    semantic fuzzy matching is allowed. If this returns False the evidence
    is rejected — a fabricated quote can never reach the report.
    """
    q = normalize_text(quote)
    t = normalize_text(raw_text)
    if len(q) < 4:
        return False
    return q in t


# A *section label* is a claim about what a number measures, so a number has to
# earn its section. Measured failure (2026-11-20 live run): the extractor was
# asked "市场规模有多大" and returned "GPT-6 Astra 在 ExploitBench 达 100%"
# labelled [市场规模], because the article it read happened to be retrieved by a
# market-size query. Every one of the ten ledger rows came back as 市场规模,
# five of them benchmark scores or success rates.
#
# The rule here is deliberately narrow: it only gates rows that carry a number
# (prose evidence cannot assert a wrong magnitude), and it fails OPEN when the
# section name is not recognised, so an unusual plan can never silently discard
# the whole ledger.
_SECTION_ANCHORS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("市场规模", "市场分析", "市场", "规模"),
     ("规模", "市场", "营收", "收入", "销售额", "产值", "出货量",
      "渗透率", "增速", "亿元", "万亿", "亿美元")),
    (("竞争格局", "竞争", "格局"),
     ("份额", "排名", "位居", "龙头", "厂商", "竞争", "集中度", "cr",
      "第一梯队", "格局")),
    (("商业模式", "定价"),
     ("商业模式", "定价", "订阅", "计费", "收费", "报价", "毛利", "客单价")),
    (("技术路线", "技术"),
     ("技术", "架构", "模型", "算法", "框架", "协议", "工具调用", "多智能体",
      "准确率", "成功率", "得分", "benchmark", "基准")),
    (("商业机会", "未来机会", "机会"),
     ("机会", "融资", "投资", "增长点", "需求", "创业", "渗透")),
    (("风险分析", "风险", "监管", "合规"),
     ("风险", "监管", "合规", "办法", "规定", "安全", "处罚", "约束", "泄露")),
    (("行业背景", "背景"),
     ("背景", "政策", "规划", "目标", "发展", "阶段", "白皮书", "报告")),
    (("政策",),
     ("政策", "规划", "目标", "国务院", "工信部", "部委", "文件", "试点")),
)


def section_anchors(section: str) -> tuple[str, ...]:
    """Anchor terms a numeric fact must contain to belong in `section`.

    Returns () when the section is unrecognised - callers treat that as
    "no gate", so an unexpected dimension name never drops evidence.

    Section names are matched by substring against several aliases per group.
    The planner's own vocabulary differs from the display names ("市场分析" vs
    "市场规模"); before the aliases existed, "市场分析" resolved to no anchors at
    all, which silently disabled the gate on the most common section.
    """
    name = _PUNCT_RE.sub("", section or "").lower()
    if not name:
        return ()
    for keys, anchors in _SECTION_ANCHORS:
        if any(key in name for key in keys):
            return anchors
    return ()


# Market size is money or a rate. A count of things is not a market
# measurement, yet a keyword anchor can be satisfied by incidental wording:
# a live run filed the procurement threshold "服务企业客户数量超过 1000 家或
# 年营收规模达亿元级" as [市场规模] 1000 家, because "营收规模" appeared in the
# same sentence. The unit of the extracted number is the tell.
_MARKET_UNITS = ("元", "%", "％", "百分点", "倍")
_COUNT_UNITS = ("家", "个", "项", "台", "人", "款", "条", "次", "起", "位", "名")


def _is_market_section(name: str) -> bool:
    return any(key in name for key in _SECTION_ANCHORS[0][0])


def evidence_belongs_to_section(ev: Evidence) -> tuple[bool, str]:
    """True when a numeric fact actually substantiates its section."""
    if ev.numeric_value is None:
        return True, ""
    anchors = section_anchors(ev.section)
    if not anchors:
        return True, ""
    unit = (ev.numeric_unit or "").strip()
    name = _PUNCT_RE.sub("", ev.section or "")
    if _is_market_section(name) and unit:
        market = any(m in unit for m in _MARKET_UNITS)
        counted = any(unit.endswith(c) for c in _COUNT_UNITS)
        if counted and not market:
            return False, f"unit_is_count:{unit}"
    haystack = _PUNCT_RE.sub("", f"{ev.claim} {ev.quote}").lower()
    if any(a in haystack for a in anchors):
        return True, ""
    return False, f"section_mismatch:{ev.section}"


def verify_evidence(state: ResearchState) -> None:
    """Run all deterministic checks; mutate state in place.

    Rejections are recorded with a reason so the planner can see *why* and
    issue a targeted follow-up query instead of blindly re-searching.
    """
    docs = {d.key: d for d in state.documents}
    kept: list[Evidence] = []
    for ev in state.evidence:
        doc = docs.get(ev.source_url) or next(
            (d for d in state.documents if d.url == ev.source_url), None
        )
        if doc is None:
            state.rejected_evidence.append((ev.id, "source_missing"))
            continue
        if not quote_in_source(ev.quote, doc.raw_text):
            state.rejected_evidence.append((ev.id, "quote_not_in_source"))
            continue
        kept.append(ev)
    state.evidence = kept
    state.conflicts = detect_numeric_conflicts(state.evidence)
    state.log(
        "verify",
        f"kept={len(kept)} rejected={len(state.rejected_evidence)} "
        f"conflicts={len(state.conflicts)}",
    )


def _numeric_key(topic: str, unit: str) -> str:
    return f"{_PUNCT_RE.sub('', topic).lower()}|{unit.strip().lower()}"


def detect_numeric_conflicts(evidence: Iterable[Evidence]) -> list[Conflict]:
    """Flag incompatible numbers for the same (topic, unit).

    Example: two sources both claim '2026 中国 AI Agent 市场规模' in 亿元 but
    say 320 and 285. Difference > 15% → conflict. This is a cheap, explainable
    proxy for cross-validation that works without an LLM judge.
    """
    groups: dict[str, list[Evidence]] = defaultdict(list)
    for ev in evidence:
        if ev.numeric_value is None or not ev.topic or not ev.numeric_unit:
            continue
        groups[_numeric_key(ev.topic, ev.numeric_unit)].append(ev)

    conflicts: list[Conflict] = []
    for key, items in groups.items():
        if len(items) < 2:
            continue
        # Compare each item against the median to avoid one outlier producing
        # a combinatorial explosion of pairwise conflicts.
        ordered = sorted(items, key=lambda e: e.numeric_value or 0.0)
        mid = len(ordered) // 2
        median = ordered[mid].numeric_value or 0.0
        for ev in ordered:
            value = ev.numeric_value or 0.0
            if median == 0:
                continue
            if abs(value - median) / abs(median) > 0.08:
                other = next((o for o in ordered if o is not ev and o.numeric_value == median), None)
                if other is None:
                    continue
                topic = ev.topic or key
                unit = ev.numeric_unit
                if not any(
                    c.topic == topic and c.unit == unit
                    and {c.left.id, c.right.id} == {ev.id, other.id}
                    for c in conflicts
                ):
                    conflicts.append(Conflict(topic=topic, unit=unit, left=ev, right=other))
    return conflicts


def validate_citations(report: str, evidence: Iterable[Evidence]) -> dict:
    """Deterministic citation audit over the finished report.

    Returns unresolved markers (fabricated refs), orphan evidence (gathered
    but never used), and a coverage ratio. This is the last gate before a
    report is allowed to leave the system.
    """
    def _num(eid: str) -> int:
        return int(eid.lstrip("Ee") or 0)

    ids = {ev.id: ev for ev in evidence}
    cited = [int(n) for n in CITATION_RE.findall(report)]
    unresolved = sorted({n for n in cited if f"E{n}" not in ids})
    used = {f"E{n}" for n in cited if f"E{n}" in ids}
    orphans = sorted(set(ids) - used, key=_num)
    total = len(ids)
    return {
        "citations": len(cited),
        "unresolved": unresolved,
        "orphans": orphans,
        "coverage": round(len(used) / total, 3) if total else 0.0,
        "passed": not unresolved and total > 0,
    }
