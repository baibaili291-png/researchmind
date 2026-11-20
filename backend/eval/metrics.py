"""Evaluation metrics — all deterministic, no LLM judgment required.

Metrics:
- quote_fidelity: fraction of evidence whose quote appears verbatim in source
- citation_resolvability: fraction of runs where every [E#] resolves
- fact_coverage: fraction of reference facts appearing in the report
- conflict_disclosure: fraction of detected conflicts disclosed in the report
"""

from __future__ import annotations

from typing import Any

from backend.core.verify import quote_in_source


def quote_fidelity(evidence: list[dict[str, Any]], raw_texts: dict[str, str]) -> float:
    if not evidence:
        return 0.0
    ok = 0
    for ev in evidence:
        text = raw_texts.get(ev.get("source_url", ""), "")
        if text and quote_in_source(ev.get("quote", ""), text):
            ok += 1
    return round(ok / len(evidence), 3)


def citation_resolvability(audit: dict[str, Any]) -> float:
    return 1.0 if audit.get("passed") else 0.0


def fact_coverage(report_md: str, reference_facts: list[str]) -> float:
    if not reference_facts:
        return 0.0
    text = report_md.lower()
    hits = sum(1 for fact in reference_facts if fact.lower() in text)
    return round(hits / len(reference_facts), 3)


def conflict_disclosure(report_md: str, conflicts: list[dict[str, Any]]) -> float:
    if not conflicts:
        return 1.0
    disclosed = 0
    for c in conflicts:
        if c.get("topic", "") in report_md:
            disclosed += 1
    return round(disclosed / len(conflicts), 3)
