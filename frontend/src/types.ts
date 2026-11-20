export interface TraceEvent {
  seq: number
  node: string
  message: string
  iteration: number
  created_at: string
}

export interface Evidence {
  id: string
  section: string
  claim: string
  quote: string
  source_url: string
  numeric_value: number | null
  numeric_unit: string
}

export interface CitationAudit {
  citations: number
  unresolved: number[]
  orphans: string[]
  coverage: number
  passed: boolean
}

export interface Conflict {
  topic: string
  unit: string
  left: Evidence
  right: Evidence
}

export interface Report {
  run_id: string
  query: string
  status: string
  report_md: string
  audit: CitationAudit
  evidence: Evidence[]
  conflicts: Conflict[]
  trace: string[]
  provider_status: Record<string, string>
  error: string | null
}
