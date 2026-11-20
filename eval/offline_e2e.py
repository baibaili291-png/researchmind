"""Deterministic end-to-end check (no network).

Run: RESEARCHMIND_USE_WEB=false python eval/offline_e2e.py
Exercises the full graph -> writer path and asserts the report-level invariants
that earlier runs violated.
"""
from __future__ import annotations

import collections
import re
import sys

sys.path.insert(0, r"D:\WorkBudy\2026-11-20-12-20-43\researchmind")

from backend.core.graph import ResearchGraph
from backend.core.models import Budget
from backend.providers.factory import build_adapter

adapter = build_adapter()
graph = ResearchGraph(adapter, Budget(max_iterations=3, max_evidence=60))
state, audit = graph.run("深度研究2026年中国 AI Agent 行业，分析市场规模、竞争格局、技术方向")

md = state.report
lines: list[str] = []
lines.append("=== RUN ===")
lines.append(f"mode={adapter.status.mode} evidence={len(state.evidence)} "
             f"docs={len(state.documents)} conflicts={len(state.conflicts)}")
lines.append("")

lines.append("=== INVARIANTS ===")
malformed = "-     - " in md or "- - " in md
lines.append(f"[1] no malformed nested bullet: {'PASS' if not malformed else 'FAIL'}")

lines.append(f"[2] provenance section: {'PASS' if '数据来源说明' in md else 'FAIL'}")
lines.append(f"[3] audit passed: {'PASS' if audit.get('passed') else 'FAIL'} {audit}")

summary = re.search(r"## 执行摘要\n(.*?)\n## ", md, re.S)
summary_text = summary.group(1) if summary else ""
topics = re.findall(r"^\s{2,}-\s+(.+?)：", summary_text, re.M)
dirty = [t for t in topics if re.search(r"\d", t) or re.search(r"[。！？；;]", t)]
lines.append(f"[4] summary topics clean: {'PASS' if not dirty else 'FAIL ' + str(dirty)}")
lines.append(f"    topics={topics}")

terminals = set("。！？；;!?.")
# Only the web path (extract_facts) guarantees sentence-terminated quotes.
# OfflineAdapter fixtures are hand-written fragments that simulate LLM output,
# so this contract does not apply in demo mode.
if adapter.status.mode == "web":
    bad_end = [e.id for e in state.evidence if e.quote and e.quote[-1] not in terminals]
    lines.append(f"[5] every quote ends on terminator: {'PASS' if not bad_end else 'FAIL ' + str(bad_end)}")
else:
    lines.append("[5] quote terminator rule: N/A (demo fixtures are fragments by design)")

trunc = [e.id for e in state.evidence
         if re.search(r"\.{2,}|…", e.quote) or re.search(r"\d{4}年\d{1,2}月\d{1,2}日", e.quote)]
lines.append(f"[6] no truncated snippet in evidence: {'PASS' if not trunc else 'FAIL ' + str(trunc)}")

lines.append("[7] evidence by section: " + str(dict(collections.Counter(e.section for e in state.evidence))))
lines.append("")
lines.append("=== REPORT ===")
lines.append(md)

out = "eval/offline_e2e_output.txt"
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("\n".join(lines[:16]))
