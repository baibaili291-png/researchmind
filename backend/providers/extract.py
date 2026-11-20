"""Deterministic fact extraction — no LLM required.

Design rationale: an LLM extracts better claims, but requiring a key means the
product cannot demonstrate its core value out of the box. Sentence-level
scoring over numeric density + query-term overlap finds exactly the sentences a
research report needs (market sizes, growth rates, shares, targets), and every
quote is by construction a verbatim span of the fetched body.

This module never invents content: it selects existing sentences. If nothing
scores above threshold, it returns nothing — and the graph reports an honest
evidence gap rather than padding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.config import settings
from backend.core.models import Document, Evidence, SubQuestion

# Sentence terminators. The half-width "." must be included because SERP
# snippets use it as a full stop ("1747亿元.中国AI营销…"), and omitting it made
# metric_subject walk back across a sentence boundary and return the garbage
# topic "或超1747亿元.中国AI营销". The (?!\d) guard keeps decimal numbers
# ("182.34亿元") intact.
_SENT_END_RE = re.compile(r"[。！？；;!?]|\.(?!\d)")
# SERP truncation markers. A search snippet is cut mid-word ("…AI营销Age..."),
# and that fragment was once promoted to evidence. Everything from the first
# truncation marker onward is dropped before sentence splitting.
_TRUNC_RE = re.compile(r"\.{2,}|…")
# A usable sentence must end on a real terminator; otherwise it is a cut tail.
# The half-width "." MUST be here too: _SENT_END_RE treats it as a full stop
# ("1747亿元.中国AI营销…"), and omitting it made every such SERP sentence fail
# its own terminal check, silently dropping ALL snippet evidence.
_TERMINALS = set("。！？；;!?.")
# Numeric fact patterns. Each group is a measurable quantity worth reporting.
# The optional thousands separator must come FIRST in the alternation, otherwise
# "400,000家" matches only the trailing "000" and yields a wrong value.
_NUM_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(亿元|万亿|亿美元|万美元|万亿元|万元|亿|万|%|％|个百分点|倍|家|人|次|个|款)"
)
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")
# Sentences that are pure boilerplate even when they contain numbers.
_BOILER_RE = re.compile(
    r"(免责声明|特别声明|风险提示|本文[^，。]{0,6}(来源|来源为)|不构成投资建议|"
    r"版权所有|转载请注明|点击.{0,4}(关注|查看)|扫码|广告)"
)
_CLEANUP_RE = re.compile(r"\s+")


@dataclass
class ScoredSentence:
    text: str
    score: int
    numbers: list[tuple[str, str]]


def split_sentences(body: str) -> list[str]:
    """Split body into complete sentences, keeping their terminal punctuation.

    Two guarantees that matter for SERP snippets:
    1. A truncation marker means content is missing *at that point* — not that
       everything after it is useless. Snippets very often START with "..." (the
       engine quoted a middle passage); treating that as "drop everything" made
       the whole document yield zero sentences, so a live run produced an empty
       report even though the snippet held real numbers. Each run between
       markers is scanned independently instead.
    2. Inside a run only segments ending on a real terminator are kept, so a
       mid-word tail ("…AI营销Age") or a date footer can never become evidence.
    """
    out: list[str] = []
    for run in _TRUNC_RE.split(body):
        start = 0
        for match in _SENT_END_RE.finditer(run):
            segment = run[start:match.end()]
            start = match.end()
            text = _CLEANUP_RE.sub(" ", segment).strip(" \n\t")
            if 18 <= len(text) <= 320 and text[-1] in _TERMINALS:
                out.append(text)
        # Any trailing run without a terminator is a cut fragment: dropped.
    return out


def parse_number(raw: str) -> float | None:
    """Parse a numeric literal, tolerating thousands separators.

    Without the comma strip, "400,000" raised ValueError and the value was
    silently dropped to None; with a naive regex it once parsed as 0.0.
    """
    if raw is None:
        return None
    cleaned = raw.replace(",", "").replace("，", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def score_sentence(sentence: str, keywords: list[str]) -> tuple[int, list[tuple[str, str]]]:
    """Score by numeric density, query-term overlap, and fact-verb presence.

    The gates below exist because of an observed failure: an off-topic sentence
    ("这篇文章讲了天气很好") cleared the threshold purely by matching a keyword,
    and was then written into the report as evidence. Numbers plus a fact verb
    are the signal that a sentence carries a reportable claim.
    """
    numbers = _NUM_RE.findall(sentence)
    lowered = sentence.lower()
    term_hits = sum(1 for k in keywords if k and k.lower() in lowered)

    score = 0
    if numbers:
        score += 26 + min(len(numbers) * 9, 30)
    score += term_hits * 7
    has_fact_verb = bool(re.search(
        r"(增长|同比|环比|占比|达到|超过|预计|预测|规模|份额|渗透率|复合|目标|排名|位居)",
        sentence,
    ))
    if has_fact_verb:
        score += 12
    if _YEAR_RE.search(sentence):
        score += 6
    if _BOILER_RE.search(sentence):
        score -= 60
    if len(sentence) < 26:
        score -= 10

    # Hard gates: without these, prose that merely mentions a keyword becomes
    # "evidence".
    if not numbers and not has_fact_verb:
        score = min(score, 0)
    if term_hits == 0 and not numbers:
        score = min(score, 0)
    return score, numbers


def expand_keywords(keywords: list[str]) -> list[str]:
    """Flatten multi-word keywords into matchable atoms.

    A live run showed keywords like "中国 AI Agent" never matched real Chinese
    copy (which writes it without spaces, or as 智能体), so term overlap scoring
    silently degraded to number-matching alone. Splitting on whitespace and
    adding the common Chinese rendering restores the signal.
    """
    atoms: list[str] = []
    for kw in keywords:
        if not kw:
            continue
        kw = kw.strip()
        if not kw:
            continue
        atoms.append(kw)
        for part in kw.split():
            if len(part) >= 2:
                atoms.append(part)
    deduped = list(dict.fromkeys(atoms))
    # "AI Agent" style phrases should also match the Chinese term used in copy.
    if any("agent" in a.lower() for a in deduped) and "智能体" not in deduped:
        deduped.append("智能体")
    return deduped


# Metric words mark the END of a subject noun phrase. We locate them and walk
# backwards to the clause start. The previous capture-group regex failed on two
# real sentences: it split "市场规模" into subject "市场" + metric "规模"
# (degrading the topic key to the generic "市场"), and its character class
# excluded full-width parentheses, so "AI AGENT（人工智能体）行业市场规模" lost
# the subject entirely. Longest metric word first so 市场规模 beats 市场.
_METRIC_WORDS = (
    "市场规模", "渗透率", "增长率", "出货量",
    "占比", "份额", "规模", "营收", "收入", "市场",
)
# Clause boundaries: the subject is searched for inside the current clause only.
# Whitespace is deliberately NOT a delimiter — "企业级AI Agent" is one subject,
# and treating the space as a boundary truncated it to just "Agent". Spaces are
# removed later by _CLEANUP_RE, which is what makes "AI Agent" and "AI  Agent"
# produce the same topic key.
_DELIM_RE = re.compile(r"[，,。；;：:！!？?、《》]+")
# Parenthetical glosses are appositives, not part of the topic key.
_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
# Trailing modifiers that are not part of the measured subject.
_SUBJECT_STOPWORDS = (
    "预计", "预测", "到", "年", "月",
    "的", "约", "达", "达到", "整体", "全行业",
)
# Trailing metric/scope words left over after locating the metric word, e.g.
# "AI营销Agent市场" -> "AI营销Agent". Order matters: longest first.
_METRIC_TAIL_RE = re.compile(
    r"(?:市场规模|应用市场|整体市场|行业市场|市场|行业|领域|产业)+$"
)
# Geographic/scope prefixes are normalized away so that "中国AI营销Agent" and
# "AI营销Agent" land on the same topic key and are actually compared.
_GEO_PREFIX_RE = re.compile(r"^(?:中国|全球|全国|国内|我国|亚太|世界)+")
# Leading noise to strip from the captured subject: attribution and dates.
# The attribution alternation MUST be anchored with ^: an unanchored pattern
# let the regex start mid-subject, so "企业级AI Agent规模预计达232亿元" had
# "企业级AI预计" deleted and degraded to just "Agent".
_LEAD_NOISE_RE = re.compile(
    r"^(?:"
    r"(?:[^，,。；;：:]{0,12}?(?:显示|报告|获悉|指出|预计|预测|称|表示|发布))"
    r"|(?:到?\s*20\d{2}\s*年?)"
    r"|(?:[0-9]{1,2}月)"
    r"|(?:[，,。；;：:]+)"
    r")+"
)
_ATTR_RE = re.compile(r"^.{0,12}?(?:显示|报告|获悉|指出|预计|预测|称|表示|发布)")


def metric_subject(sentence: str) -> str:
    """Extract what a number measures, e.g. "AI营销Agent" from
    "2026年中国AI营销Agent市场规模达636亿元".

    Strategy: find each metric word, take the clause before it, remove
    parenthetical glosses, strip attribution/date/geo noise, then strip any
    leftover metric tail. Longest surviving candidate wins. Without this,
    three *different* markets (AI营销Agent / AI数字人 / 数字经济) collapsed
    onto one topic key and were reported as contradicting each other.
    """
    best = ""
    for word in _METRIC_WORDS:
        start = 0
        while True:
            idx = sentence.find(word, start)
            if idx < 0:
                break
            start = idx + 1
            prefix = sentence[:idx]
            # Restrict the backwards walk to the current sentence first, then to
            # the current clause. Cutting at the last sentence terminator is what
            # stops the subject from absorbing a preceding sentence.
            ends = list(_SENT_END_RE.finditer(prefix))
            prefix = prefix[ends[-1].end():] if ends else prefix
            delims = list(_DELIM_RE.finditer(prefix))
            clause = prefix[delims[-1].end():] if delims else prefix
            clause = _PAREN_RE.sub("", clause).strip()
            if not clause:
                continue
            # Attribution is only stripped when it starts the clause; otherwise
            # a verb appearing mid-subject would eat the subject itself.
            if _ATTR_RE.match(clause):
                clause = _ATTR_RE.sub("", clause).strip()
            candidate = _LEAD_NOISE_RE.sub("", clause).strip()
            candidate = _GEO_PREFIX_RE.sub("", candidate).strip()
            candidate = _METRIC_TAIL_RE.sub("", candidate).strip()
            for stop in _SUBJECT_STOPWORDS:
                if candidate.endswith(stop) and len(candidate) > len(stop):
                    candidate = candidate[: -len(stop)]
            # Collapse internal spaces so "AI Agent" and "AI  Agent" key alike.
            candidate = _CLEANUP_RE.sub("", candidate.strip("，,。、：: "))
            if len(candidate) >= 2 and len(candidate) > len(best):
                best = candidate
    return best


def _derive_topic(sentence: str, numbers: list[tuple[str, str]],
                  keywords: list[str], section: str) -> str:
    """Topic key used to group numbers across sources for conflict detection.

    Keyed on the measured subject plus unit, so only genuinely comparable
    figures are compared. Falling back to the first matched keyword (the old
    behaviour) produced false conflicts between unrelated markets.
    """
    unit = numbers[0][1] if numbers else ""
    subject = metric_subject(sentence)
    if not subject:
        matched = [k for k in keywords if k and k.lower() in sentence.lower()]
        subject = matched[0] if matched else "general"
    return f"{section}:{subject}:{unit}"


def _clean_claim(sentence: str) -> str:
    claim = sentence.strip()
    # Drop trailing source attributions that are not part of the fact.
    claim = re.sub(r"[（(](?:来源|数据|记者|编辑)[:：][^）)]*[）)]", "", claim)
    claim = re.sub(r"(观点网讯|据悉|记者了解到)[:：]?", "", claim)
    return claim.strip() or sentence


def extract_facts(doc: Document, subq: SubQuestion, keywords: list[str]) -> list[Evidence]:
    """Select the highest-scoring verbatim sentences as evidence."""
    body = doc.raw_text
    terms = expand_keywords(keywords)
    scored: list[ScoredSentence] = []
    for sentence in split_sentences(body):
        score, numbers = score_sentence(sentence, terms)
        if score >= settings.fact_min_score:
            scored.append(ScoredSentence(text=sentence, score=score, numbers=numbers))
    scored.sort(key=lambda s: (-s.score, len(s.text)))

    # Drop near-duplicate sentences (same first numeric value + similar prefix).
    chosen: list[ScoredSentence] = []
    seen_nums: set[tuple[str, str]] = set()
    for item in scored:
        key = item.numbers[0] if item.numbers else (item.text[:16], "")
        if key in seen_nums:
            continue
        seen_nums.add(key)
        chosen.append(item)
        if len(chosen) >= settings.max_facts_per_doc:
            break

    out: list[Evidence] = []
    for item in chosen:
        value: float | None = None
        unit = ""
        if item.numbers:
            raw, unit = item.numbers[0]
            value = parse_number(raw)
        claim = _clean_claim(item.text)
        out.append(Evidence(
            id="",
            sub_question_id=subq.id,
            section=subq.section,
            claim=claim[:180],
            quote=item.text,
            source_url=doc.url,
            source_title=doc.title,
            published_at=doc.published_at,
            source_type=doc.source_type,
            topic=_derive_topic(item.text, item.numbers, terms, subq.section),
            numeric_value=value,
            numeric_unit=unit,
            confidence=round(min(0.45 + item.score / 160.0, 0.95), 3),
        ))
    return out


__all__ = ["extract_facts", "score_sentence", "split_sentences"]
