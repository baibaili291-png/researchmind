"""Evaluation harness — runs the question bank and reports deterministic metrics.

Usage:
    python -m backend.eval.harness
    python -m backend.eval.harness --type multi_source --output results.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.core.graph import ResearchGraph
from backend.core.models import Budget
from backend.eval.metrics import (
    citation_resolvability,
    conflict_disclosure,
    fact_coverage,
    quote_fidelity,
)
from backend.providers.factory import build_adapter

QUESTIONS = Path(__file__).with_name("questions.json")


def load_questions(qtype: str | None = None) -> list[dict[str, Any]]:
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    return [q for q in data if not qtype or q["type"] == qtype]


def run_one(question: dict[str, Any]) -> dict[str, Any]:
    adapter = build_adapter()
    graph = ResearchGraph(adapter, Budget(max_iterations=3))
    state, audit = graph.run(question["query"])

    raw_texts = {d.url: d.raw_text for d in state.documents}
    evidence = [ev.to_dict() for ev in state.evidence]
    conflicts = [
        {"topic": c.topic, "unit": c.unit}
        for c in state.conflicts
    ]
    return {
        "id": question["id"],
        "type": question["type"],
        "query": question["query"],
        "status": "completed" if state.report else "failed",
        "metrics": {
            "quote_fidelity": quote_fidelity(evidence, raw_texts),
            "citation_resolvability": citation_resolvability(audit),
            "fact_coverage": fact_coverage(state.report, question.get("reference_facts", [])),
            "conflict_disclosure": conflict_disclosure(state.report, conflicts),
        },
        "evidence_count": len(evidence),
        "conflict_count": len(conflicts),
        "audit": audit,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchMind evaluation harness")
    parser.add_argument("--type", choices=["unique_answer", "multi_source", "insufficient_info"])
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    questions = load_questions(args.type)
    results = [run_one(q) for q in questions]

    summary: dict[str, float] = {}
    for key in ("quote_fidelity", "citation_resolvability", "fact_coverage", "conflict_disclosure"):
        vals = [r["metrics"][key] for r in results]
        summary[key] = round(sum(vals) / len(vals), 3) if vals else 0.0

    output = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(results),
        "summary": summary,
        "results": results,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"结果已写入 {args.output}")
    else:
        print(text)


if __name__ == "__main__":
    main()
