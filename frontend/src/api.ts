import type { Report, TraceEvent } from './types'

export async function startRun(
  query: string,
  budget: Record<string, number>,
): Promise<{ run_id: string }> {
  const res = await fetch('/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, budget }),
  })
  if (!res.ok) throw new Error(`启动失败: ${res.status}`)
  return res.json()
}

export function subscribe(
  runId: string,
  onProgress: (event: TraceEvent) => void,
  onDone: () => void,
  onError: (message: string) => void,
): () => void {
  const es = new EventSource(`/runs/${runId}/events`)
  es.addEventListener('progress', (e) => onProgress(JSON.parse((e as MessageEvent).data)))
  es.addEventListener('done', () => { es.close(); onDone() })
  es.addEventListener('error', () => { es.close(); onError('事件流中断') })
  return () => es.close()
}

export async function fetchReport(runId: string): Promise<Report> {
  const res = await fetch(`/runs/${runId}/report`)
  if (!res.ok) throw new Error('报告尚未就绪')
  return res.json()
}
