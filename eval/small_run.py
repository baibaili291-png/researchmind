"""Small-budget live run: prove the pipeline produces real evidence when an
engine is available, without exhausting a rate-limited engine.

Run: python eval/small_run.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = "eval/small_run_output.txt"

payload = {
    "query": "2026年中国AI Agent 市场规模与竞争格局",
    "budget": {"max_iterations": 1, "max_searches": 3, "max_fetches": 6, "max_evidence": 20},
}
req = urllib.request.Request(
    BASE + "/runs", data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}, method="POST",
)
rid = json.loads(urllib.request.urlopen(req, timeout=15).read())["run_id"]
info: dict = {}
for _ in range(900):
    info = json.loads(urllib.request.urlopen(f"{BASE}/runs/{rid}", timeout=15).read())
    if info.get("status") not in ("queued", "running"):
        break
    time.sleep(0.5)

rep = json.loads(urllib.request.urlopen(f"{BASE}/runs/{rid}/report", timeout=25).read())
md = rep["report_md"]
lines = [
    f"status={info.get('status')} docs={len(rep['documents'])} evidence={len(rep['evidence'])}",
    "provenance=" + json.dumps(rep.get("provenance", {}), ensure_ascii=False),
    "",
    "=== SOURCES ===",
]
for d in rep["documents"]:
    lines.append(f"  [{d.get('fetch_method')}] {d['url'][:80]}")

lines.append("")
lines.append("=== EVIDENCE ===")
for ev in rep["evidence"]:
    val = f"{ev['numeric_value']}{ev['numeric_unit'] or ''}" if ev.get("numeric_value") is not None else ""
    lines.append(f"  {ev['id']} [{ev['section']}] {val}")
    lines.append(f"      {ev['quote'][:150]}")
    lines.append(f"      topic={ev.get('topic')}")

lines.append("")
lines.append("=== OFF-TOPIC CHECK ===")
banned = ("两会", "政府工作报告", "节假日", "统计年鉴", "人鱼", "世界杯", "以旧换新", "国债")
leaks = [ev["id"] for ev in rep["evidence"]
         if any(b in ev["quote"] for b in banned)]
lines.append("off-topic evidence: " + ("NONE" if not leaks else str(leaks)))

lines.append("")
lines.append("=== REPORT (first 60 lines) ===")
lines.extend(md.splitlines()[:60])

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print("\n".join(lines[:34]))
