"""End-to-end demo of the Phase 1 skeleton.

Run: python -m researchmind.demo
"""

from __future__ import annotations

from .adapters import OfflineAdapter
from .graph import ResearchGraph
from .ledger import EvidenceLedger
from .models import Budget, ResearchState


def main() -> None:
    adapter = OfflineAdapter()
    graph = ResearchGraph(adapter, Budget(max_iterations=3, max_evidence=120))
    state, audit = graph.run("深度研究2026年中国 AI Agent 行业")

    ledger = EvidenceLedger()
    ledger.start_run("demo-run-1", state.query, "{}", state.started_at.isoformat())
    ledger.save_documents(state.documents)
    ledger.save_evidence(state.evidence)
    ledger.finish_run(audit)

    print("=" * 68)
    print("TRACE")
    print("=" * 68)
    for line in state.trace:
        print(line)

    print()
    print("=" * 68)
    print(f"RESULT  evidence={len(state.evidence)}  "
          f"rejected={len(state.rejected_evidence)}  conflicts={len(state.conflicts)}")
    print(f"CITATION AUDIT  {audit}")
    print(f"LEDGER  {ledger.counts()}")
    print("=" * 68)
    print()
    print(state.report)


if __name__ == "__main__":
    main()
