# 部署与运行

## 模式一：零依赖本地运行（默认）

适用于快速验证、演示、无外网环境。

```bash
python -m backend.api.server --port 8000
```

- 不需要 API key，不需要 pip install。
- 搜索/抓取/LLM 均使用内置离线适配器（固定资料库覆盖 AI Agent 行业主题）。
- 前端静态页面随服务一起提供：`http://localhost:8000`。

## 模式二：真实数据 + FastAPI

```bash
pip install -r requirements.txt
cp deploy/.env.example .env
# 在 .env 中设置：
#   RESEARCHMIND_USE_LIVE_PROVIDERS=true
#   RESEARCHMIND_LLM_API_KEY=...
#   RESEARCHMIND_SEARCH_API_KEY=...
export $(grep -v '^#' .env | xargs)
uvicorn backend.api.app:app --host 0.0.0.0 --port 8000
```

真实模式下的降级链：

- LLM：任意 OpenAI 兼容端点（DeepSeek/Qwen/OpenAI/vLLM），通过 `RESEARCHMIND_LLM_BASE_URL` 切换。
- 搜索：Tavily。
- 抓取：HTTP + trafilatura；正文过短时启用 Playwright；再失败走 Reader API。
- 合规：`RESEARCHMIND_RESPECT_ROBOTS=true` 时检查 robots.txt，并按域名限速。

## 模式三：Docker Compose

```bash
cd deploy
docker compose up --build
```

包含三个服务：

- `api`：FastAPI 服务，端口 8000。
- `postgres`：pgvector 镜像，自动执行 `backend/sql/schema.sql`，端口 5432。
- `redis`：缓存，端口 6379。

## 前端

静态前端由后端直接提供，无需构建。如需 React 版本：

```bash
cd frontend
npm install
npm run dev      # 开发模式，代理到 localhost:8000
npm run build    # 产出 dist/
```

## 验证命令

```bash
python -m unittest discover -s backend/tests -v
python -m backend.core.demo
python -m backend.eval.harness --output eval/results.json
```

## 配置参考

所有配置项及默认值见 `deploy/.env.example` 与 `backend/config.py`。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RESEARCHMIND_USE_LIVE_PROVIDERS` | `false` | 是否启用真实 provider |
| `RESEARCHMIND_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `RESEARCHMIND_SEARCH_MAX_RESULTS` | `6` | 每次搜索返回条数 |
| `RESEARCHMIND_FETCH_TIMEOUT` | `15` | 抓取超时（秒） |
| `RESEARCHMIND_RESPECT_ROBOTS` | `true` | 是否遵守 robots.txt |
| `RESEARCHMIND_DOMAIN_DELAY` | `1.0` | 同域名抓取间隔（秒） |
