"""Research planning: decompose a user question into a research plan.

With an LLM key this calls the model. Without one, a dimension-based planner
still produces a real plan: it detects the research subject from the query by
stripping instruction verbs, the target year, and research-dimension words,
then instantiates the standard deep-research dimensions with queries that
combine the detected subject and dimension keywords.

This keeps the product genuinely functional offline instead of returning a
hard-coded plan for any input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.core.models import SubQuestion

# Instruction verbs / politeness wrappers — never part of the subject.
_INSTRUCTION_RE = re.compile(
    r"(?:请|帮我|帮忙|麻烦|你|我需要|我想|想要|希望)?"
    r"(?:深度|深入|详细|全面|系统|快速)?(?:研究|调研|分析|梳理|了解|综述|盘点|考察)"
    r"(?:一下)?"
)
# Research-dimension words: these become the *angle* of a query, not the subject.
_DIMENSION_RE = re.compile(
    r"(?:的|与|和|及|、|，|,)?\s*"
    r"(?:市场规模|市场|竞争格局|格局|主要企业|主要厂商|头部厂商|厂商|"
    r"技术方向|技术路线|技术趋势|技术|商业模式|盈利模式|未来机会|商业机会|"
    r"机会|风险|挑战|发展趋势|趋势|前景|政策|监管|合规|产业链|"
    r"份额|定价|收费|应用场景|应用|落地|渗透率|增长|增速|规模)"
)
_TIME_RE = re.compile(r"(20\d{2})\s*年?")
_YEAR_ANY_RE = re.compile(r"20\d{2}")
_SPLIT_RE = re.compile(r"[\s,，。、;；:：()（）\[\]【】\"'“”]+")
# Possessive particle only when it sits between two word characters, so that
# sentence-level fillers are dropped ("深圳的跨境电商" -> "深圳跨境电商").
_POSSESSIVE_RE = re.compile(r"(?<=[\u4e00-\u9fffA-Za-z0-9])的(?=[\u4e00-\u9fffA-Za-z0-9])")
# Words where 的 is lexical, not a particle. A pure lookaround cannot tell
# "深圳的跨境" from "目的意义", so these are masked before stripping — without
# the mask, "研究目的与意义" degraded to "目与意义".
_PROTECTED_DI = ("目的", "的确", "标的", "的士", "真的", "假的")
_MASK_TOKEN = "\x00DI\x00"


def strip_possessive(text: str) -> str:
    """Remove the particle 的 between words while protecting lexical 的."""
    if "的" not in text:
        return text
    masked = text
    for word in _PROTECTED_DI:
        masked = masked.replace(word, word.replace("的", _MASK_TOKEN))
    masked = _POSSESSIVE_RE.sub("", masked)
    return masked.replace(_MASK_TOKEN, "的")
# Generic filler that carries no retrieval value on its own.
_FILLER = {"行业", "领域", "领域内", "方面", "情况", "现状", "的", "了", "呢", "吗", "等"}

# Dimensions of a deep research report: (section, focus, query keywords).
DIMENSIONS: list[tuple[str, str, list[str]]] = [
    ("行业背景", "发展演进与政策环境", ["发展历程", "政策"]),
    ("市场分析", "市场规模与增长", ["市场规模 亿元", "增长率"]),
    ("竞争格局", "主要企业与市场份额", ["竞争格局 主要企业", "市场份额"]),
    ("技术路线", "技术方向与架构演进", ["技术方向 架构", "技术趋势"]),
    ("商业模式", "盈利方式与定价", ["商业模式 收费", "定价"]),
    ("商业机会", "增长机会与资本动向", ["商业机会 融资", "发展前景"]),
    ("风险分析", "合规、数据与落地风险", ["风险 合规 监管", "落地挑战"]),
]


@dataclass
class TopicAnalysis:
    subject: str
    year: str
    keywords: list[str]
    requested_sections: list[str]


def _detect_requested_sections(text: str) -> list[str]:
    """Sections the user explicitly asked about, in order of mention."""
    found: list[str] = []
    for section, _, dim_words in DIMENSIONS:
        probes = {section} | set(dim_words) | {section[:2]}
        for probe in probes:
            if probe and probe in text:
                found.append(section)
                break
    return found


def analyze_topic(query: str) -> TopicAnalysis:
    """Extract subject, target year, keywords and requested sections."""
    year_match = _TIME_RE.search(query)
    year = year_match.group(1) if year_match else ""

    stripped = _INSTRUCTION_RE.sub(" ", query)
    stripped = _DIMENSION_RE.sub(" ", stripped)
    stripped = _TIME_RE.sub(" ", stripped)
    stripped = _YEAR_ANY_RE.sub(" ", stripped)

    tokens = [t for t in _SPLIT_RE.split(stripped) if t]
    tokens = [t for t in tokens if t not in _FILLER and len(t) >= 1]
    # Drop a leading/trailing possessive particle: "深圳的跨境电商" -> "深圳跨境电商"
    tokens = [t.strip("的") or t for t in tokens]
    tokens = [t for t in tokens if t and t not in _FILLER]

    # Merge single-character fragments into the preceding token so "AI Agent"
    # style phrases survive splitting ("中国 AI Agent" -> one subject).
    merged: list[str] = []
    for token in tokens:
        if len(token) == 1 and merged and re.match(r"[\u4e00-\u9fff]", token):
            merged[-1] = merged[-1] + token
        else:
            merged.append(token)

    # Subject = leading tokens (the head noun phrase), not dimension words.
    subject_tokens: list[str] = []
    for token in merged:
        if token in _FILLER:
            continue
        subject_tokens.append(token)
        if len("".join(subject_tokens)) >= 14:
            break
    subject = " ".join(subject_tokens).strip() or query.strip()[:24]
    # Drop the possessive particle inside the subject ("深圳的跨境电商" ->
    # "深圳跨境电商"); it adds no retrieval value and can hurt exact matching.
    # strip_possessive (not the raw regex) is used so lexical 的 survives.
    subject = strip_possessive(subject).strip() or subject

    keywords = [t for t in dict.fromkeys(merged) if len(t) >= 2][:10]
    keywords = [strip_possessive(k) or k for k in keywords]
    if subject and subject not in keywords:
        keywords.insert(0, subject)
    return TopicAnalysis(
        subject=subject,
        year=year,
        keywords=keywords,
        requested_sections=_detect_requested_sections(query),
    )


def build_plan(query: str, max_sections: int = 7) -> list[SubQuestion]:
    """Template planner: subject x dimensions -> sub-questions with queries."""
    analysis = analyze_topic(query)
    subject = analysis.subject
    year = analysis.year or "2026"
    # Drop keywords already contained in the subject, otherwise queries repeat
    # words ("中国 AI Agent 中国 政策") and waste the search budget.
    subject_keywords = [
        k for k in analysis.keywords
        if k != subject and k not in subject and subject not in k
    ][:3]

    # Honour explicitly requested sections first, then fill remaining budget.
    order = list(DIMENSIONS)
    if analysis.requested_sections:
        requested = [d for d in order if d[0] in analysis.requested_sections]
        rest = [d for d in order if d[0] not in analysis.requested_sections]
        order = requested + rest

    plan: list[SubQuestion] = []
    for i, (section, focus, dim_keywords) in enumerate(order[:max_sections], start=1):
        prefix = f"{year} {subject}" if year else subject
        queries = [f"{prefix} {dim_keywords[0]}".strip()]
        if len(dim_keywords) > 1:
            # Second query broadens via an extra subject keyword when available.
            extra = subject_keywords[0] if subject_keywords else ""
            queries.append(f"{subject} {extra} {dim_keywords[1]}".replace("  ", " ").strip())
        dim_words = [w for kw in dim_keywords for w in kw.split()]
        plan.append(SubQuestion(
            id=f"Q{i}",
            section=section,
            question=f"{subject}的{focus}是什么？",
            queries=queries,
            keywords=[subject, *subject_keywords, *dim_words],
        ))
    return plan


__all__ = ["build_plan", "analyze_topic", "DIMENSIONS", "TopicAnalysis"]
