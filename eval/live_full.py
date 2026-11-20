"""Full-budget live run: the closest thing to what a user actually clicks.

Uses the default budget (3 iterations / 24 searches) so every planned
dimension is searched, and writes a full transcript for inspection.
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
OUT = "eval/live_full_output.txt"

payload = {"query": "2026年中国AI Agent市场规模与竞争格局"}


def get(path: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    req = urllib.request.Request(
        BASE + "/runs", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    rid = json.loads(urllib.request.urlopen(req, timeout=20).read())["run_id"]

    info: dict = {}
    for _ in range(1200):
        info = get(f"/runs/{rid}")
        if info.get("status") not in ("queued", "running"):
            break
        time.sleep(1.0)

    rep = get(f"/runs/{rid}/report", timeout=60)
    md = rep["report_md"]
    prov = rep.get("provenance", {})

    lines = [
        f"status={info.get('status')} docs={len(rep['documents'])} evidence={len(rep['evidence'])}",
        "provenance=" + json.dumps(prov, ensure_ascii=False),
        "",
        "=== TRACE ===",
    ]
    for step in info.get("trace", []) or []:
        lines.append(f"  {step}")

    lines += ["", "=== SOURCES ==="]
    for d in rep["documents"]:
        lines.append(f"  [{d.get('fetch_method')}] {d['url'][:100]}")
        lines.append(f"      {d['title'][:80]}")

    lines += ["", "=== EVIDENCE ==="]
    for ev in rep["evidence"]:
        val = f"{ev['numeric_value']}{ev['numeric_unit'] or ''}" if ev.get("numeric_value") is not None else ""
        lines.append(f"  {ev['id']} [{ev['section']}] {val}")
        lines.append(f"      claim: {ev['claim'][:110]}")
        lines.append(f"      quote: {ev['quote'][:110]}")
        lines.append(f"      topic: {ev.get('topic')}")

    lines += ["", "=== SECTION SANITY ==="]
    # Use the pipeline's own gate instead of a hand-written word list: the list
    # version produced false alarms on legitimate 渗透率 rows (twice), which is
    # worse than no check - it trains you to ignore the warning.
    import sys as _sys
    _sys.path.insert(0, ".")
    from backend.core.models import Evidence as _Ev
    from backend.core.verify import evidence_belongs_to_section as _gate

    bad = []
    for ev in rep["evidence"]:
        probe = _Ev(id=ev["id"], sub_question_id=ev.get("sub_question_id", ""),
                    section=ev["section"], claim=ev["claim"], quote=ev["quote"],
                    source_url=ev["source_url"], source_title=ev["source_title"],
                    published_at=ev.get("published_at"), source_type=ev.get("source_type", ""),
                    topic=ev.get("topic", ""), numeric_value=ev.get("numeric_value"),
                    numeric_unit=ev.get("numeric_unit", ""), confidence=0.9)
        ok, why = _gate(probe)
        if not ok:
            bad.append(f"{ev['id']}({why})")
    lines.append("evidence that fails its own section gate: " + ("NONE" if not bad else str(bad)))

    lines += ["", "=== REPORT ===", md]
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

    print(f"status={info.get('status')} docs={len(rep['documents'])} evidence={len(rep['evidence'])}")
    print("provenance=" + json.dumps(prov, ensure_ascii=False))
    print("sections=" + str(sorted({ev["section"] for ev in rep["evidence"]})))
    print("unsupported numbers: " + ("NONE" if not bad else str(bad)))
    print(f"written -> {OUT} ({len(md)} chars of report)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
