"""Live web-research smoke run. Writes a report sample and a metrics summary.

Usage: python eval/live_run.py "<query>"
"""

from __future__ import annotations

import collections
import sys
import time
from pathlib import Path

# Allow running as a script from any CWD: put the project root on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.core.graph import ResearchGraph  # noqa: E402
from backend.core.models import Budget  # noqa: E402
from backend.providers.factory import build_adapter  # noqa: E402


def main() -> None:
    query = (sys.argv[1] if len(sys.argv) > 1
             else "深度研究2026年中国 AI Agent 行业的市场规模与竞争格局")
    adapter = build_adapter()
    graph = ResearchGraph(adapter, Budget(
        max_iterations=2, max_searches=10, max_hits=40,
        max_fetches=14, max_evidence=60,
    ))

    started = time.time()
    state, audit = graph.run(query)

    lines: list[str] = []
    lines.append(f"elapsed: {time.time() - started:.0f}s")
    lines.append("--- TRACE ---")
    lines.extend(state.trace)
    lines.append(f"\n--- DOCUMENTS: {len(state.documents)} ---")
    for d in state.documents:
        lines.append(
            f"  [{d.fetch_method}] {len(d.raw_text):5d} chars {d.source_type:9s} "
            f"{d.title[:38]} | {d.url[:52]}"
        )
    lines.append(f"\n--- EVIDENCE: {len(state.evidence)} ---")
    lines.append("by section: " + str(dict(collections.Counter(e.section for e in state.evidence))))
    for e in state.evidence:
        value = f"{e.numeric_value}{e.numeric_unit}" if e.numeric_value is not None else "-"
        lines.append(f"  {e.id:4s}({e.section}) [{value}] {e.claim[:80]}")
    lines.append(f"\nconflicts: {len(state.conflicts)}  rejected: {len(state.rejected_evidence)}")
    lines.append("audit: " + str(audit))
    lines.append("fetch stats: " + str(dict(adapter.fetch_stats)))
    lines.append(f"report chars: {len(state.report)}")

    with open("eval/live_run_output.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    with open("eval/real_report_sample.md", "w", encoding="utf-8") as fh:
        fh.write(state.report)
    print("DONE")


if __name__ == "__main__":
    main()
