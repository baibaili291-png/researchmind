# ResearchMind Phase 1 技术设计（可直接落地的最小版本）

> 完整实现见 `backend/`，Web 界面见 `frontend/`，回归测试见 `backend/tests/`，生产库表见 `backend/sql/schema.sql`。
> 核心用纯标准库实现，零依赖即可运行；生产 FastAPI 接口与真实 provider 适配器已一并提供。

---

## 1. 一句话技术路线

把"研究"实现为一个**带环的状态机**，把"证据"实现为**一等公民数据表**，把"验证"实现为**确定性纯函数**。
其余（搜索、抓取、写作）都是可替换的适配器。

**核心不变量**：报告里每一句事实性陈述都必须能解析到证据账本中的一条记录；每条记录的 quote 必须是原文的子串。
这两条由代码保证，不依赖 prompt。

---

## 2. 模块边界

| 模块 | 职责 | 生产实现 | 骨架对应 |
|---|---|---|---|
| 编排层 | 状态机、循环、预算、断点续跑 | LangGraph `StateGraph` + `AsyncPostgresSaver` | `src/graph.py` |
| 数据模型 | 强类型状态与证据 | Pydantic v2 + TypedDict | `src/models.py` |
| 适配器 | 搜索 / 抓取 / 抽取 / 写作 | Tavily / httpx+trafilatura+Playwright / DeepSeek | `src/adapters.py` |
| 校验层 | 引文子串、数字冲突、引用可解析 | 纯函数（无 LLM） | `src/verify.py` |
| 存储层 | 证据账本、运行事件、审计 | PostgreSQL + asyncpg | `src/ledger.py` |

**为什么校验层不用 LLM**：确定性函数可测试、可回放、零延迟、零成本。LLM 只负责"生成候选"，
不负责"判定真伪"。交叉验证里的语义判断（如"A 是否支持 B"）放在**冲突已定位之后**的小范围内做，
而不是对全文做一次空泛的 Reflection。

---

## 3. 状态机（关键：环在哪里）

```
plan → search → fetch → extract → verify → route ─┐
         ↑                                       │
         └──────────── follow_up_queries ─────────┘
                                                  └→ write → cite → END
```

- `route` 是条件边，三种回退触发：**数字冲突**、**子问题无证据**、**引文被拒**（quote 不在原文）。
- 回退时不是盲目重搜，而是生成 `follow_up_queries`（例如"市场规模 统计口径 差异 说明"）。
- `Budget` 在每次循环里检查：最大迭代 3、最大搜索 24、最大抓取 40、最大证据 120、最大 LLM 调用 60、时限 600s。
  任一触顶 → 停止循环，写**含覆盖率披露的部分报告**，状态置 `partial`，而不是静默截断。

骨架里 `ResearchGraph.run()` 完整实现了这条控制流；demo 输出中可以看到因 320亿/285亿 冲突而触发的回退。

---

## 4. 证据账本（关键：从第一天就有）

数据模型见 `sql/schema.sql`，核心是三张表：

- `document`：原始网页、规范化 URL、正文、内容哈希、抓取方式、来源类型（一手/二手）。
  `UNIQUE(run_id, canonical_url)` + `content_hash` 双重去重，解决转载造成的"伪交叉验证"。
- `evidence`：**原子主张 + 原文引述 + 来源 + 数值**。每条证据只表达一个可核查的事实。
- `citation_audit`：每次成稿的引用审计结果（引用数、悬空引用、孤儿证据、覆盖率）。

**写入顺序**：`extract` 产出候选 → `verify` 过滤（引文必须是原文子串）→ 才允许写入账本 →
`write` 只能从账本取素材 → `cite` 做最终审计。这个顺序保证了幻觉无法进入报告。

**pgvector 推迟**：单次研究的语料是有界的，检索靠 `topic/numeric_unit` 索引 + LLM 筛选足够。
只有当"跨多次研究复用证据库"成为真实需求时，再加 `embedding` 列和 HNSW 索引。

---

## 5. 确定性校验的三个纯函数

1. **`quote_in_source(quote, raw_text)`** — NFKC + 空白归一后的子串匹配。伪造引文直接拒绝。
2. **`detect_numeric_conflicts(evidence)`** — 按 (topic, unit) 分组，与中位数偏差 > 8% 记为冲突。
   例：320亿 vs 285亿 → 冲突 → 报告显式披露两种口径，而不是取平均或二选一。
3. **`validate_citations(report, evidence)`** — 解析 `[E123]` 标记：
   - `unresolved`：报告引用了账本里不存在的 ID（幻觉引用）→ 必须为空。
   - `orphans`：账本里有证据但报告没用 → 提示覆盖率问题。
   - `coverage`：已用证据 / 全部证据。demo 中达到 1.0。

这三项都是纯函数，可单测、可回放。`tests/test_core.py` 中 12 项测试覆盖了伪造引文、数字冲突、
悬空引用、孤儿证据、URL 归一和端到端审计。

---

## 6. 适配器与抓取降级链（生产接缝）

`src/adapters.py` 定义了 `ResearchAdapters` 协议，生产按以下顺序替换：

```
httpx (async, 15s timeout, UA header)
  → trafilatura 正文抽取
  → 正文 < 200 字或疑似 JS 渲染 → Playwright headless
  → 403/429 或抽取失败 → 第三方 Reader API（如 r.jina.ai）
  → 存 raw_text + content_hash + fetch_method
```

- **来源权重**：`primary`（政府/机构/原始报告）> `secondary`（媒体/转载）。冲突时优先展示一手来源。
- **成本**：Tavily 按次计费，单次研究上限 24 次搜索；正文抽取优先本地库，Reader API 只作兜底。
- **合规**：抓取前检查 robots.txt，同域名限速，正文仅用于研究，报告只引用 URL 与短引述。

---

## 7. API 与进度流（生产）

```
POST /runs            {query, budget?} → {run_id}
GET  /runs/{id}/events                 → SSE: node / message / seq
GET  /runs/{id}/report                 → Markdown + audit
GET  /runs/{id}/evidence?section=      → 证据账本查询
```

- 长任务走后台 worker（ Celery / ARQ / LangGraph 自带 checkpointer），HTTP 请求立即返回 `run_id`。
- `run_event` 表持久化每一步事件，断线重连可从 `seq` 续播。
- 前端渲染**研究轨迹**（当前子问题、已抓来源、冲突提示）而非只有转圈——这是可信度的一部分。

---

## 8. 评测（从第一天就建，20–30 题）

| 指标 | 计算方式 | 目标 |
|---|---|---|
| 引文保真率 | `quote_in_source` 通过的证据 / 全部证据 | 100% |
| 引用可解析率 | `unresolved == 0` 的运行 / 全部运行 | 100% |
| 事实覆盖率 | 参考答案要点被报告覆盖的比例 | ≥ 85% |
| 冲突披露率 | 已标注冲突 / 人工标注应标注冲突 | ≥ 90% |
| 成本/时延 | P50 / P95 token、耗时、费用 | 设上限并回归 |

题库分三类：**有唯一答案**（监管条款）、**有多口径**（市场规模，考察冲突披露）、**信息不足**（考察是否诚实标注不足）。
每次改 prompt / 换模型都全量回归，指标落库对比，而不是靠"看起来变好了"。

---

## 9. 分阶段落地顺序

1. **Phase 1（本骨架）**：状态机 + 证据账本 + 三个确定性校验 + 离线适配器 + 引用审计 + 12 项测试。
   验收标准：伪造引文进不了报告；任何运行都能给出可解析的引用与覆盖率。
2. **Phase 2**：接真实 Tavily / DeepSeek / httpx+trafilatura；FastAPI + SSE；PostgreSQL + checkpointer 断点续跑；人在环确认计划。
3. **Phase 3**：语义相关性判断（小范围 LLM judge）、跨运行证据复用（pgvector）、题库与自动评测、成本治理。

**RAG 排最后不是遗漏**：当语料只在单次运行内有界时，向量库带来的复杂度大于收益；
先靠账本的结构化字段 + 确定性校验把可信度做扎实。

---

## 10. 本地运行

```bash
cd researchmind
python -m src.demo                          # 端到端演示，含冲突检测与引用审计
python -m unittest discover -s tests -v     # 12 项回归测试
```

无需任何 API key 或第三方依赖（Python 3.10+）。
