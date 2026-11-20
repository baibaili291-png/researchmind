"""End-to-end invariant check against the live server.

Run: python eval/final_check.py
Writes eval/final_check_output.txt and prints a short verdict.
"""
from __future__ import annotations

import collections
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = "eval/final_check_output.txt"

payload = {
    "query": "深度研究2026年中国 AI Agent 行业的市场规模与竞争格局",
    "budget": {"max_iterations": 1, "max_searches": 6, "max_fetches": 6, "max_evidence": 40},
}
req = urllib.request.Request(
    BASE + "/runs",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
rid = json.loads(urllib.request.urlopen(req, timeout=15).read())["run_id"]

status = "unknown"
run_info: dict = {}
# A live run with rate-limit backoff can take minutes; polling too briefly made
# an unfinished run look like a 404 (record.state is still None).
for _ in range(1400):
    run_info = json.loads(urllib.request.urlopen(f"{BASE}/runs/{rid}", timeout=15).read())
    status = run_info.get("status", "unknown")
    if status not in ("queued", "running"):
        break
    time.sleep(0.5)

try:
    rep = json.loads(urllib.request.urlopen(f"{BASE}/runs/{rid}/report", timeout=30).read())
except urllib.error.HTTPError as exc:
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(f"REPORT FETCH FAILED: {exc}\nstatus={status}\nrun_info={json.dumps(run_info, ensure_ascii=False)}\n")
    raise SystemExit(f"report unavailable: status={status} run_info={run_info}")
md = rep["report_md"]
prov = rep.get("provenance", {})
docs = rep.get("documents", [])
evidence = rep.get("evidence", [])
audit = rep.get("audit", {})

lines: list[str] = []
lines.append("=== RUN ===")
lines.append(f"status={status} docs={len(docs)} evidence={len(evidence)}")
lines.append("run_info=" + json.dumps(run_info, ensure_ascii=False))
lines.append("provenance=" + json.dumps(prov, ensure_ascii=False))
lines.append("")

lines.append("=== INVARIANTS ===")

# 1. web mode must never contain fabricated fixtures
fixtures = [d["url"] for d in docs if "example.com" in d["url"] or "example.cn" in d["url"]]
lines.append(f"[1] no fabricated fixture sources: {'PASS' if not fixtures else 'FAIL ' + str(fixtures)}")

# 2. markdown list rendering must not nest bullets
malformed = "-     - " in md or "- - " in md
lines.append(f"[2] no malformed nested bullet: {'PASS' if not malformed else 'FAIL'}")

# 3. provenance disclosure present
lines.append(f"[3] provenance section rendered: {'PASS' if '数据来源说明' in md else 'FAIL'}")

# 4. citation audit
lines.append(f"[4] citation audit passed: {'PASS' if audit.get('passed') else 'FAIL'} {json.dumps(audit, ensure_ascii=False)}")

# 5. executive-summary sub-bullet topics must be clean (no digits, no sentence fragments)
summary_match = re.search(r"## 执行摘要\n(.*?)\n## ", md, re.S)
summary = summary_match.group(1) if summary_match else ""
topics = re.findall(r"^\s{2,}-\s+(.+?)：", summary, re.M)
dirty = [t for t in topics if re.search(r"\d", t) or re.search(r"[。！？；;]", t)]
lines.append(f"[5] summary topics clean: {'PASS' if not dirty else 'FAIL ' + str(dirty)}")
lines.append(f"    topics={topics}")

# 6. truncation markers / date footers must never appear inside evidence quotes
trunc_leaks = [
    e["id"] for e in evidence
    if re.search(r"\.{2,}|…", e.get("quote", "")) or re.search(r"\d{4}年\d{1,2}月\d{1,2}日", e.get("quote", ""))
]
lines.append(f"[6] no truncated snippet in evidence: {'PASS' if not trunc_leaks else 'FAIL ' + str(trunc_leaks)}")

# 7. every evidence quote ends on a real terminator
terminals = set("。！？；;!?.")
bad_end = [e["id"] for e in evidence if e.get("quote") and e["quote"][-1] not in terminals]
lines.append(f"[7] every quote ends on terminator: {'PASS' if not bad_end else 'FAIL ' + str(bad_end)}")

# 8. evidence spread across sections
lines.append("[8] evidence by section: " + str(dict(collections.Counter(e["section"] for e in evidence))))

lines.append("")
lines.append("=== SOURCES ===")
for d in docs[:12]:
    lines.append(f"  [{d.get('fetch_method', '?')}] {d.get('url', '')[:80]}")
lines.append("")
lines.append("=== REPORT ===")
lines.append(md)

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

print("\n".join(lines[:28]))
