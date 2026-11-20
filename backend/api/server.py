"""Zero-dependency HTTP server implementing the same API as app.py.

Runs the entire project on bare Python (3.10+) with no pip installs:
    python -m backend.api.server --port 8000

Static frontend is served from frontend/ at http://localhost:8000/
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from backend.api.service import service
from backend.providers.search import configured_engine_names

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchMind/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # keep console clean; events are visible in the web UI

    # -- helpers -------------------------------------------------------------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ext = path.suffix.lower()
        types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".json": "application/json; charset=utf-8",
        }
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _sse(self, event: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- routes --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/health":
            return self._send_json({"status": "ok", "service": "researchmind"})
        if path == "/mode":
            from backend.config import settings
            return self._send_json({
                "mode": "demo" if not settings.use_web else "web",
                "use_web": settings.use_web,
                "llm_configured": bool(settings.llm_api_key),
                "search_key_configured": bool(settings.search_api_key),
                # DeepSeek's own Search/Fetch is the only stable retrieval path
                # on this network, so its state is what actually decides whether
                # live research can produce evidence.
                "deepseek_search_configured": bool(settings.deepseek_api_key),
                # Derived from the real chain, never a hand-kept copy.
                "search_engines": configured_engine_names(),
            })
        if path == "/settings":
            # 上次用过的研究预算，前端打开页面时用它回填输入框。
            return self._send_json({"budget": service.last_budget})
        if path == "/runs":
            # 历史运行列表：前端刷新/重开页面后靠它把最近一份报告找回来。
            return self._send_json({"runs": service.list_runs()})
        if path == "/" or path == "/index.html":
            return self._send_file(FRONTEND / "index.html")
        if path.startswith("/static/"):
            return self._send_file(FRONTEND / path[len("/static/"):])
        if path.startswith("/runs/") and path.endswith("/events"):
            run_id = path.split("/")[2]
            if not service.get(run_id):
                return self._send_json({"error": "run not found"}, 404)
            self._start_sse()
            last = int(query.get("after", ["0"])[0])
            idle = 0
            while True:
                events = service.events_since(run_id, last)
                for event in events:
                    last = event["seq"]
                    self._sse("progress", event)
                if service.is_finished(run_id) and not events:
                    self._sse("done", {"run_id": run_id})
                    break
                if not events:
                    idle += 1
                    if idle > 300:
                        self._sse("error", {"message": "timeout"})
                        break
                else:
                    idle = 0
                time.sleep(1.0)
            return
        if path.startswith("/runs/") and path.endswith("/report"):
            run_id = path.split("/")[2]
            report = service.report(run_id)
            return self._send_json(report or {"error": "not ready"}, 200 if report else 404)
        if path.startswith("/runs/") and path.endswith("/evidence"):
            run_id = path.split("/")[2]
            report = service.report(run_id)
            if not report:
                return self._send_json({"error": "not ready"}, 404)
            section = query.get("section", [None])[0]
            items = report["evidence"]
            if section:
                items = [e for e in items if e["section"] == section]
            return self._send_json(items)
        if path.startswith("/runs/"):
            run_id = path.split("/")[2]
            record = service.get(run_id)
            if not record:
                return self._send_json({"error": "run not found"}, 404)
            return self._send_json({
                "run_id": record.id,
                "query": record.query,
                "status": record.status,
                "events": len(record.events),
                "error": record.error,
                # The per-node trace is the only place that explains *why* a run
                # came back thin: which engine refused, how many dimensions got
                # evidence, how many facts were dropped by the section gate.
                # Without it the answer to "why is my report empty" is
                # unanswerable from the outside.
                "trace": (record.state.trace[-40:] if record.state else []),
            })
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/runs":
            body = self._body_json()
            query = str(body.get("query", "")).strip()
            if len(query) < 4:
                return self._send_json({"error": "query too short"}, 400)
            budget = body.get("budget") or {}
            run_id = service.start(query, budget if isinstance(budget, dict) else {})
            return self._send_json({"run_id": run_id, "status": "queued"})
        self.send_error(404)


def main() -> None:
    parser = argparse.ArgumentParser(description="ResearchMind zero-dependency server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ResearchMind running at http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
