-- ResearchMind Phase 1 — PostgreSQL schema
-- Single store for runs, evidence and audits. pgvector deferred intentionally:
-- enable it only when evidence needs to be reused across research runs.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
-- CREATE EXTENSION IF NOT EXISTS vector;  -- deferred to Phase 3

CREATE TABLE research_run (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running','completed','failed','partial')),
    budget_json   JSONB NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    total_cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0
);
CREATE INDEX idx_run_started ON research_run(started_at DESC);

CREATE TABLE document (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    url           TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title         TEXT,
    published_at  TIMESTAMPTZ,
    raw_text      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    fetch_method  TEXT NOT NULL CHECK (fetch_method IN ('http','playwright','reader_api')),
    source_type   TEXT NOT NULL CHECK (source_type IN ('primary','secondary','unknown')),
    http_status   INTEGER,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, canonical_url)
);
CREATE INDEX idx_document_hash ON document(run_id, content_hash);

CREATE TABLE sub_question (
    id            TEXT NOT NULL,
    run_id        UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    section       TEXT NOT NULL,
    question      TEXT NOT NULL,
    queries_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE evidence (
    id              TEXT NOT NULL,
    run_id          UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    document_id     UUID NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    sub_question_id TEXT NOT NULL,
    section         TEXT NOT NULL,
    claim           TEXT NOT NULL,
    quote           TEXT NOT NULL,
    quote_offset    INTEGER,
    source_url      TEXT NOT NULL,
    source_title    TEXT,
    published_at    TIMESTAMPTZ,
    source_type     TEXT NOT NULL,
    topic           TEXT,
    numeric_value   NUMERIC,
    numeric_unit    TEXT,
    confidence      REAL NOT NULL DEFAULT 1.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, id),
    FOREIGN KEY (run_id, sub_question_id)
        REFERENCES sub_question(run_id, id)
);
CREATE INDEX idx_evidence_section ON evidence(run_id, section);
CREATE INDEX idx_evidence_topic ON evidence(run_id, topic, numeric_unit)
    WHERE numeric_value IS NOT NULL;

CREATE TABLE conflict (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    topic        TEXT NOT NULL,
    unit         TEXT NOT NULL,
    left_id      TEXT NOT NULL,
    right_id     TEXT NOT NULL,
    resolution   TEXT NOT NULL DEFAULT 'unresolved'
                 CHECK (resolution IN ('unresolved','disclosed','resolved')),
    note         TEXT,
    FOREIGN KEY (run_id, left_id)  REFERENCES evidence(run_id, id),
    FOREIGN KEY (run_id, right_id) REFERENCES evidence(run_id, id)
);

CREATE TABLE report_version (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    content_md  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, version)
);

CREATE TABLE citation_audit (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    report_id   UUID NOT NULL REFERENCES report_version(id),
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    citations   INTEGER NOT NULL,
    unresolved  JSONB NOT NULL DEFAULT '[]'::jsonb,
    orphans     JSONB NOT NULL DEFAULT '[]'::jsonb,
    coverage    REAL NOT NULL,
    passed      BOOLEAN NOT NULL
);

CREATE TABLE run_event (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL REFERENCES research_run(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    node       TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

-- Phase 3, only when cross-run reuse is actually needed:
-- ALTER TABLE evidence ADD COLUMN embedding vector(1024);
-- CREATE INDEX ON evidence USING hnsw (embedding vector_cosine_ops);
