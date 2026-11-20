# 评测说明

## 运行

```bash
python -m backend.eval.harness                 # 全部 20 题
python -m backend.eval.harness --type multi_source
python -m backend.eval.harness --output eval/results.json
```

## 题库结构（`backend/eval/questions.json`）

| 类型 | 题数 | 考察目标 |
|---|---|---|
| `unique_answer` | 8 | 有唯一答案的事实题，考察引用是否指向权威来源 |
| `multi_source` | 7 | 多口径行业题，考察冲突披露与引用覆盖率 |
| `insufficient_info` | 5 | 信息不足题，考察系统是否诚实标注而非编造 |

## 指标

- **quote_fidelity**：证据引述在原文中逐字出现的比例。目标 1.0。
- **citation_resolvability**：报告中所有 `[E#]` 都能解析到证据账本的运行比例。目标 1.0。
- **fact_coverage**：参考答案要点在报告中出现的比例。目标 ≥ 0.85（在线模式下）。
- **conflict_disclosure**：已检测到的口径冲突在报告中被显式披露的比例。目标 ≥ 0.9。

## 当前基线结果（离线适配器）

```
quote_fidelity:        1.000
citation_resolvability: 1.000
fact_coverage:         0.169
conflict_disclosure:    1.000
```

**fact_coverage 偏低是预期结果，不是缺陷**：离线适配器的固定资料库只覆盖
"2026 中国 AI Agent 行业"这一个主题，其余 19 题没有对应资料，系统正确地输出了
"（本轮未收集到可验证证据）"而不是编造内容。这恰好验证了信息不足类题目的诚实性。

要得到有业务意义的 fact_coverage，需要设置环境变量启用真实 provider：

```bash
export RESEARCHMIND_USE_LIVE_PROVIDERS=true
export RESEARCHMIND_LLM_API_KEY=...
export RESEARCHMIND_SEARCH_API_KEY=...
python -m backend.eval.harness --output eval/results_live.json
```

每次修改 prompt 或换模型后都应重跑评测，对比 `eval/results*.json` 中的指标变化。
