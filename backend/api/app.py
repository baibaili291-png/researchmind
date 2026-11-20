"""Production FastAPI application with SSE progress streaming.

Requires: pip install fastapi uvicorn
For a zero-dependency equivalent that runs on bare Python, see server.py.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - depends on optional deps
    raise RuntimeError(
        "FastAPI is not installed. Run `pip install -r requirements.txt` "
        "or use `python -m backend.api.server` for the zero-dependency server."
    ) from exc

from backend.api.service import service
from backend.providers.search import configured_engine_names

app = FastAPI(title="ResearchMind API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RunRequest(BaseModel):
    query: str = Field(..., min_length=4, max_length=500)
    budget: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "researchmind"}


@app.get("/mode")
async def mode() -> dict[str, Any]:
    """Report whether this instance does real web research or runs on fixtures.

    The UI shows this up front so a reader never mistakes demo data for live
    research results.
    """
    from backend.config import settings

    return {
        "mode": "demo" if not settings.use_web else "web",
        "use_web": settings.use_web,
        "llm_configured": bool(settings.llm_api_key),
        "search_key_configured": bool(settings.search_api_key),
        "deepseek_search_configured": bool(settings.deepseek_api_key),
        # Derived from the real chain: a hand-kept copy had already drifted
        # (listed baidu first, omitted Sogou entirely).
        "search_engines": configured_engine_names(),
    }


@app.post("/runs")
async def create_run(req: RunRequest) -> dict[str, str]:
    run_id = service.start(req.query, req.budget)
    return {"run_id": run_id, "status": "queued"}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    record = service.get(run_id)
    if not record:
        raise HTTPException(404, "run not found")
    return {
        "run_id": record.id,
        "query": record.query,
        "status": record.status,
        "events": len(record.events),
        "error": record.error,
        # Same rationale as server.py: the trace is what makes a thin run
        # diagnosable (engine refusals, dimension coverage, gate drops).
        "trace": (record.state.trace[-40:] if record.state else []),
    }


@app.get("/runs/{run_id}/events")
async def stream_events(run_id: str, after: int = 0) -> StreamingResponse:
    if not service.get(run_id):
        raise HTTPException(404, "run not found")

    async def generator():
        last = after
        idle = 0
        while True:
            events = service.events_since(run_id, last)
            for event in events:
                last = event["seq"]
                yield f"event: progress\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if service.is_finished(run_id) and not events:
                yield f"event: done\ndata: {json.dumps({'run_id': run_id})}\n\n"
                break
            if not events:
                idle += 1
                if idle > 300:  # ~5 minutes at 1s intervals
                    yield f"event: error\ndata: {json.dumps({'message': 'timeout'})}\n\n"
                    break
            else:
                idle = 0
            await asyncio.sleep(1.0)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/runs/{run_id}/report")
async def get_report(run_id: str) -> dict[str, Any]:
    report = service.report(run_id)
    if not report:
        raise HTTPException(404, "report not ready or run not found")
    return report


@app.get("/runs/{run_id}/evidence")
async def get_evidence(run_id: str, section: str | None = None) -> list[dict[str, Any]]:
    report = service.report(run_id)
    if not report:
        raise HTTPException(404, "run not found")
    items = report["evidence"]
    if section:
        items = [e for e in items if e["section"] == section]
    return items
