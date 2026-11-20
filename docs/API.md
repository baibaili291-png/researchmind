# API 文档

两套实现共享同一套路由：生产用 `backend/api/app.py`（FastAPI），
零依赖用 `backend/api/server.py`（Python 标准库 `http.server`）。

## `GET /health`

健康检查。返回 `{"status": "ok", "service": "researchmind"}`。

## `POST /runs`

启动一个研究任务，立即返回 `run_id`，任务在后台线程执行。

请求体：

```json
{
  "query": "深度研究2026年中国 AI Agent 行业",
  "budget": {
    "max_iterations": 3,
    "max_searches": 24,
    "max_evidence": 120
  }
}
```

响应：

```json
{"run_id": "877e4d273812", "status": "queued"}
```

## `GET /runs/{run_id}`

查询任务状态：`run_id`、`query`、`status`、`events`、`error`。
状态取值：`queued` / `running` / `completed` / `completed_with_conflicts` / `partial` / `failed`。

## `GET /runs/{run_id}/events`（SSE）

流式推送研究轨迹。每个事件：

```
event: progress
data: {"seq": 1, "node": "plan", "message": "decomposed into 4 sub-questions", "iteration": 0, "created_at": "..."}
```

结束时推送 `event: done`。查询参数 `after=N` 可从第 N 个事件后继续（断线重连）。

## `GET /runs/{run_id}/report`

返回完整报告：

```json
{
  "run_id": "...",
  "query": "...",
  "status": "completed_with_conflicts",
  "report_md": "# ... [E1] ...",
  "audit": {"citations": 7, "unresolved": [], "orphans": [], "coverage": 1.0, "passed": true},
  "evidence": [{"id": "E1", "section": "市场分析", "claim": "...", "quote": "...", "source_url": "..."}],
  "conflicts": [{"topic": "...", "unit": "亿元", "left": {...}, "right": {...}}],
  "trace": ["[0] plan: ..."],
  "provider_status": {"llm": "offline", "search": "offline", "fetch": "offline"}
}
```

## `GET /runs/{run_id}/evidence?section=市场分析`

返回证据列表，可按章节过滤。

## 示例：用 curl 跑一次完整研究

```bash
# 启动任务
RUN=$(curl -s -X POST http://localhost:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"query":"深度研究2026年中国 AI Agent 行业"}')
ID=$(echo $RUN | python -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

# 看实时轨迹
curl -N http://localhost:8000/runs/$ID/events

# 取报告
curl -s http://localhost:8000/runs/$ID/report > report.json
```
