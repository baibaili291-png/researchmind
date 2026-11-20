# ResearchMind

自主研究与深度调研 Agent —— 输入一个研究问题，自动完成规划、检索、证据抽取、交叉验证与报告生成，
报告中每条事实性陈述都带可追溯的 `[E#]` 引用标记。

## 快速开始（零依赖，立即可跑）

不需要 API key，不需要 pip install，只需要 Python 3.10+：

```bash
# 1. 启动 Web 服务
python -m backend.api.server --port 8000

# 2. 浏览器打开
#    http://localhost:8000
```

Web 界面包含：研究任务输入、实时研究轨迹（SSE）、带引用的报告、证据账本、引用审计与冲突披露。

命令行方式：

```bash
python -m backend.core.demo                       # 端到端演示
python -m unittest discover -s backend/tests -v  # 回归测试
python -m backend.eval.harness                    # 20 题评测
```

## 启用真实数据与模型

```bash
cp deploy/.env.example .env
# 编辑 .env，设置 RESEARCHMIND_USE_LIVE_PROVIDERS=true 和两个 API key
pip install -r requirements.txt
uvicorn backend.api.app:app --host 0.0.0.0 --port 8000
```

也可以用 Docker：

```bash
cd deploy && docker compose up --build
```

## 项目结构（全部内容在此单一文件夹）

```
researchmind/
├── README.md                    本文件
├── requirements.txt             生产依赖（离线模式不需要）
├── backend/
│   ├── config.py                环境变量配置与安全默认值
│   ├── core/                    状态机、数据模型、确定性校验、证据账本
│   │   ├── models.py            强类型数据结构、URL 规范化
│   │   ├── verify.py            引文子串 / 数字冲突 / 引用可解析（纯函数）
│   │   ├── graph.py             plan→search→fetch→extract→verify→route→write→cite
│   │   ├── adapters.py          适配器协议 + 离线实现
│   │   ├── ledger.py            SQLite 证据账本
│   │   └── demo.py              命令行演示
│   ├── providers/               真实 provider 与故障降级
│   │   ├── search.py            Tavily 搜索
│   │   ├── fetch.py             httpx→trafilatura→Playwright→Reader API 降级链
│   │   ├── llm.py               DeepSeek/Qwen（OpenAI 兼容）结构化输出
│   │   └── factory.py           组合真实与离线适配器
│   ├── api/
│   │   ├── app.py               生产 FastAPI + SSE
│   │   ├── server.py            零依赖 HTTP 服务器（同一套 API）
│   │   └── service.py           后台 worker 与运行记录
│   ├── eval/                    题库、指标、评测脚本
│   ├── sql/schema.sql           生产 PostgreSQL 库表
│   └── tests/                   回归测试
├── frontend/
│   ├── index.html               可直接访问的静态界面
│   ├── app.js / styles.css      静态界面逻辑与样式
│   └── src/                     React + TypeScript 生产前端
├── deploy/                      Docker、Compose、环境变量样例
├── docs/                        技术设计、架构、API、部署、评测文档
└── eval/results.json            评测结果
```

## 设计要点

1. **带环状态机**：验证发现数字冲突或证据缺口时回到检索阶段，并生成针对性追问。
2. **证据账本是一等公民**：写入顺序固定为「抽取 → 校验 → 入账 → 写作 → 引用审计」。
3. **校验是确定性纯函数**：引文必须是原文子串；同主题数值偏差 > 8% 记为冲突；悬空引用必须为空。
4. **供应商可替换**：搜索、抓取、LLM 都是适配器，任一失败自动回退到离线实现。

详细设计与验证数据见 `docs/`。

## 当前状态

Phase 1 已完整实现并验证。Phase 2（LangGraph checkpointer、人在环）与 Phase 3（语义相关性判断、
跨运行证据复用 pgvector）在 `docs/TECH_DESIGN.md` 中有落地方案，尚未实现。
