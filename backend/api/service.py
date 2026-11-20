"""Run orchestration shared by the FastAPI app and the stdlib fallback server.

Owns background workers, event logs (persisted to run_event in production),
and the finished report/evidence for each run.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from backend.core.graph import ResearchGraph
from backend.core.models import Budget, ResearchState
from backend.providers.factory import build_adapter

# 跑完的报告落盘，否则重启一次进程，内存里的 run 就全没了 —— 用户明明
# 生成过报告，刷新页面却看到「等待任务」，这是最伤信任的一种失败。
RUNS_DIR = Path(os.getenv("RESEARCHMIND_RUNS_DIR", "data/runs"))

# 上次用过的那套研究预算。用户没主动改就是不想改，页面每次重开又回到
# 3/24/120 等于逼他重填一遍 —— 存服务端而不是 localStorage，换浏览器也一致。
SETTINGS_FILE = Path(os.getenv("RESEARCHMIND_SETTINGS_FILE", "data/settings.json"))


@dataclass
class RunRecord:
    id: str
    query: str
    status: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    state: ResearchState | None = None
    audit: dict[str, Any] = field(default_factory=dict)
    provider_status: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    # 从磁盘恢复的历史运行：state 已不在内存，但报告本体还在。
    cached_report: dict[str, Any] | None = None


class ResearchService:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()
        self.last_budget: dict[str, Any] = self._load_settings()
        self._load_persisted()

    # -- user settings -------------------------------------------------------
    def _load_settings(self) -> dict[str, Any]:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        budget = data.get("budget")
        return budget if isinstance(budget, dict) else {}

    def _save_settings(self, budget: dict[str, Any]) -> None:
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps({"budget": budget}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # -- persistence ---------------------------------------------------------
    def _load_persisted(self) -> None:
        """把历史运行读回内存，重启后页面仍能看到上一次的报告。"""
        if not RUNS_DIR.exists():
            return
        try:
            files = sorted(RUNS_DIR.glob("*.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:50]
        except OSError:
            return
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rid = str(data.get("run_id") or path.stem)
            self._runs[rid] = RunRecord(
                id=rid,
                query=str(data.get("query") or ""),
                status=str(data.get("status") or "completed"),
                created_at=str(data.get("created_at") or ""),
                finished_at=data.get("finished_at"),
                cached_report=data,
            )

    def _persist(self, record: RunRecord) -> None:
        """落盘一份报告。raw_text 动辄上万字且前端不用，写盘前丢掉。"""
        try:
            payload = self.report(record.id)
            if not payload:
                return
            for doc in payload.get("documents") or []:
                doc.pop("raw_text", None)
            payload["created_at"] = record.created_at
            payload["finished_at"] = record.finished_at
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            (RUNS_DIR / f"{record.id}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except (OSError, TypeError, ValueError):
            # 落盘失败不能影响本次运行的结果展示。
            pass

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self._runs.values(),
                           key=lambda r: (r.created_at or "", r.id), reverse=True)
        out: list[dict[str, Any]] = []
        for r in items[:limit]:
            # 证据条数用于前端挑"最值得展示"的那份：最新的一条可能只是一次
            # 小预算试跑，展示效果远不如完整研究。
            if r.cached_report:
                count = len(r.cached_report.get("evidence") or [])
            else:
                count = len(r.state.evidence) if r.state is not None else 0
            out.append({
                "run_id": r.id,
                "query": r.query,
                "status": r.status,
                "created_at": r.created_at,
                "finished_at": r.finished_at,
                "has_report": bool(r.state or r.cached_report),
                "evidence_count": count,
            })
        return out

    def start(self, query: str, budget: dict[str, Any] | None = None) -> str:
        if budget:
            # 记住这次的预算，下次开页面直接回填。
            self.last_budget = dict(budget)
            self._save_settings(self.last_budget)
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(id=run_id, query=query)
        with self._lock:
            self._runs[run_id] = record

        thread = threading.Thread(
            target=self._worker, args=(record, budget or {}), daemon=True
        )
        thread.start()
        return run_id

    def _worker(self, record: RunRecord, budget_dict: dict[str, Any]) -> None:
        try:
            record.status = "running"
            adapter = build_adapter()
            record.provider_status = adapter.status.as_dict()
            budget = Budget(**{
                k: v for k, v in budget_dict.items()
                if k in Budget.__dataclass_fields__
            })
            graph = ResearchGraph(adapter, budget)

            def emit(node: str, message: str, iteration: int) -> None:
                event = {
                    "seq": len(record.events) + 1,
                    "node": node,
                    "message": message,
                    "iteration": iteration,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                with self._lock:
                    record.events.append(event)

            state, audit = graph.run(record.query, on_event=emit)
            record.state = state
            record.audit = audit
            record.status = "completed" if audit["passed"] else "partial"
            if state.conflicts:
                record.status = "completed_with_conflicts"
        except Exception as exc:  # keep the run record useful on failure
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {exc}"
        finally:
            record.finished_at = datetime.now(timezone.utc).isoformat()
            self._persist(record)

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def events_since(self, run_id: str, seq: int = 0) -> list[dict[str, Any]]:
        record = self.get(run_id)
        if not record:
            return []
        with self._lock:
            return [e for e in record.events if e["seq"] > seq]

    def is_finished(self, run_id: str) -> bool:
        record = self.get(run_id)
        return bool(record and record.status in {
            "completed", "completed_with_conflicts", "partial", "failed"
        })

    def report(self, run_id: str) -> dict[str, Any] | None:
        record = self.get(run_id)
        if not record:
            return None
        if not record.state:
            # 历史运行：state 已不在内存，返回落盘的那份报告。
            return record.cached_report
        state = record.state
        return {
            "run_id": run_id,
            "query": record.query,
            "status": record.status,
            "report_md": state.report,
            "audit": record.audit,
            "evidence": [ev.to_dict() for ev in state.evidence],
            "documents": [
                {
                    "url": d.url,
                    "title": d.title,
                    "published_at": d.published_at,
                    "source_type": d.source_type,
                    "fetch_method": d.fetch_method,
                    "raw_text": d.raw_text,
                }
                for d in state.documents
            ],
            "conflicts": [
                {
                    "topic": c.topic,
                    "unit": c.unit,
                    "left": c.left.to_dict(),
                    "right": c.right.to_dict(),
                }
                for c in state.conflicts
            ],
            "trace": state.trace,
            "provenance": state.provenance,
            "provider_status": record.provider_status,
            "error": record.error,
        }


service = ResearchService()
