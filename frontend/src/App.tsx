import React, { useEffect, useRef, useState } from 'react'
import { fetchReport, startRun, subscribe } from './api'
import type { Report, TraceEvent } from './types'
import './styles.css'

const NODE_LABELS: Record<string, string> = {
  start: '启动', plan: '规划', search: '检索', fetch: '抓取',
  extract: '抽取', verify: '验证', route: '路由', write: '写作', cite: '引用审计',
}

const EXAMPLES = [
  '深度研究2026年中国 AI Agent 行业，分析市场规模、主要企业、技术发展方向、商业模式和未来机会。',
  '调研 2026 年新能源汽车出海的合规要求与主要市场壁垒。',
  '分析全球大模型推理芯片的技术路线与主要厂商竞争格局。',
]

export default function App() {
  const [query, setQuery] = useState(EXAMPLES[0])
  const [maxIter, setMaxIter] = useState(3)
  const [busy, setBusy] = useState(false)
  const [events, setEvents] = useState<TraceEvent[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [tab, setTab] = useState<'report' | 'evidence' | 'audit'>('report')
  const [meta, setMeta] = useState('')
  const traceRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    traceRef.current?.scrollTo({ top: traceRef.current.scrollHeight })
  }, [events])

  async function run() {
    if (query.trim().length < 4) return
    setBusy(true)
    setEvents([])
    setReport(null)
    setMeta('正在提交任务…')
    try {
      const { run_id } = await startRun(query.trim(), {
        max_iterations: maxIter,
        max_searches: 24,
        max_evidence: 120,
      })
      setMeta(`run_id: ${run_id}`)
      subscribe(
        run_id,
        (e) => setEvents((prev) => [...prev, e]),
        async () => {
          setBusy(false)
          setReport(await fetchReport(run_id))
          setMeta((m) => `${m} · 已完成`)
        },
        (msg) => { setBusy(false); setMeta((m) => `${m} · ${msg}`) },
      )
    } catch (err) {
      setBusy(false)
      setMeta(`提交失败: ${(err as Error).message}`)
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">RM</div>
          <div>
            <div className="brand-name">ResearchMind</div>
            <div className="brand-sub">自主研究与深度调研 Agent</div>
          </div>
        </div>
        <div className="status-pill">{report?.provider_status?.llm?.startsWith('live') ? '在线模式' : '离线模式 · 零依赖'}</div>
      </header>
      <main className="layout">
        <section className="panel input-panel">
          <div className="panel-head"><h2>研究任务</h2></div>
          <textarea value={query} onChange={(e) => setQuery(e.target.value)} rows={5} />
          <div className="examples">
            {EXAMPLES.map((ex, i) => (
              <button key={i} className="chip" onClick={() => setQuery(ex)}>示例 {i + 1}</button>
            ))}
          </div>
          <div className="budget-row">
            <label>最大迭代 <input type="number" value={maxIter} min={1} max={8}
              onChange={(e) => setMaxIter(Number(e.target.value))} /></label>
          </div>
          <button className="run-btn" onClick={run} disabled={busy}>
            {busy ? '研究进行中…' : '开始研究'}
          </button>
          <div className="run-meta">{meta}</div>
        </section>

        <section className="panel">
          <div className="panel-head"><h2>研究轨迹</h2><span className="panel-hint">{events.length} 个事件</span></div>
          <div className="trace" ref={traceRef}>
            {events.map((e) => (
              <div className="trace-item" key={e.seq}>
                <span className="trace-seq">{String(e.seq).padStart(2, '0')}</span>
                <span className={`trace-node ${e.node}`}>{NODE_LABELS[e.node] || e.node}</span>
                <span className="trace-msg">{e.message}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-head">
            <h2>研究报告</h2>
            <div className="report-actions">
              {(['report', 'evidence', 'audit'] as const).map((t) => (
                <button key={t} className={`tab ${tab === t ? 'active' : ''}`} onClick={() => setTab(t)}>
                  {{ report: '报告', evidence: '证据账本', audit: '引用审计' }[t]}
                </button>
              ))}
            </div>
          </div>
          {report?.audit && (
            <div className="audit-bar">
              <span className="metric">引用 <b>{report.audit.citations}</b></span>
              <span className="metric">悬空 <b>{report.audit.unresolved.length}</b></span>
              <span className="metric">覆盖率 <b>{Math.round(report.audit.coverage * 100)}%</b></span>
              <span className={`metric ${report.audit.passed ? 'pass' : 'fail'}`}>
                <b>{report.audit.passed ? '审计通过' : '审计未通过'}</b>
              </span>
            </div>
          )}
          <div className="report-body">
            {!report ? <div className="empty-state"><p>报告生成后显示在这里</p></div> : tab === 'report' ? (
              <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{report.report_md}</pre>
            ) : tab === 'evidence' ? (
              report.evidence.map((ev) => (
                <div className="ev-card" key={ev.id}>
                  <div className="ev-card-head"><span className="ev-id">{ev.id}</span><span className="ev-section">{ev.section}</span></div>
                  <div className="ev-claim">{ev.claim}</div>
                  <div className="ev-quote">“{ev.quote}”</div>
                  <div className="ev-source"><a href={ev.source_url} target="_blank" rel="noreferrer">{ev.source_url}</a></div>
                </div>
              ))
            ) : (
              <>
                <h2>数据口径冲突</h2>
                {report.conflicts.length === 0 ? <p>未检测到冲突</p> : report.conflicts.map((c, i) => (
                  <div className="conflict-card" key={i}>
                    <strong>{c.topic}</strong><br />
                    {c.left.numeric_value}{c.unit} [{c.left.id}] vs {c.right.numeric_value}{c.unit} [{c.right.id}]
                  </div>
                ))}
              </>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}
