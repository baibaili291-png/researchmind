"""Evidence ledger schema — mirrors src/models.py 1:1.

PostgreSQL is the single store for Phase 1. pgvector is intentionally absent:
it is only needed when evidence must be reused across research runs. Add it
later with `ALTER TABLE evidence ADD COLUMN embedding vector(1024)`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .models import Document, Evidence


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_run (
    id            TEXT PRIMARY KEY,
    query         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    budget_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES research_run(id),
    url            TEXT NOT NULL,
    canonical_url  TEXT NOT NULL,
    title          TEXT,
    published_at   TEXT,
    raw_text       TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    fetch_method   TEXT,
    source_type    TEXT,
    fetched_at     TEXT NOT NULL,
    UNIQUE(run_id, canonical_url)
);

CREATE TABLE IF NOT EXISTS evidence (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES research_run(id),
    document_id      TEXT NOT NULL REFERENCES document(id),
    sub_question_id  TEXT NOT NULL,
    section          TEXT NOT NULL,
    claim            TEXT NOT NULL,
    quote            TEXT NOT NULL,
    quote_offset     INTEGER,
    source_url       TEXT NOT NULL,
    source_title     TEXT,
    published_at     TEXT,
    source_type      TEXT,
    topic            TEXT,
    numeric_value    REAL,
    numeric_unit     TEXT,
    confidence       REAL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_evidence_run_section ON evidence(run_id, section);
CREATE INDEX IF NOT EXISTS idx_evidence_topic ON evidence(run_id, topic, numeric_unit);

CREATE TABLE IF NOT EXISTS citation_audit (
    run_id       TEXT NOT NULL REFERENCES research_run(id),
    checked_at   TEXT NOT NULL,
    citations    INTEGER NOT NULL,
    unresolved   TEXT NOT NULL,
    orphans      TEXT NOT NULL,
    coverage     REAL NOT NULL,
    passed       INTEGER NOT NULL
);
"""


class EvidenceLedger:
    """SQLite implementation for the skeleton. Same SQL shape as PostgreSQL."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        self._run_id = ""

    @property
    def run_id(self) -> str:
        return self._run_id

    def start_run(self, run_id: str, query: str, budget_json: str, started_at: str) -> None:
        self._run_id = run_id
        self.conn.execute(
            "INSERT OR REPLACE INTO research_run"
            " (id, query, status, started_at, budget_json) VALUES (?,?,?,?,?)",
            (run_id, query, "running", started_at, budget_json),
        )
        self.conn.commit()

    def save_documents(self, docs: Iterable[Document]) -> int:
        n = 0
        for i, d in enumerate(docs):
            self.conn.execute(
                "INSERT OR IGNORE INTO document"
                " (id, run_id, url, canonical_url, title, published_at, raw_text,"
                "  content_hash, fetch_method, source_type, fetched_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"D{i}", self._run_id, d.url, d.key, d.title, d.published_at,
                 d.raw_text, d.content_hash, d.fetch_method, d.source_type,
                 "2026-01-01T00:00:00Z"),
            )
            n += 1
        self.conn.commit()
        return n

    def save_evidence(self, items: Iterable[Evidence]) -> int:
        n = 0
        for ev in items:
            self.conn.execute(
                "INSERT OR REPLACE INTO evidence"
                " (id, run_id, document_id, sub_question_id, section, claim, quote,"
                "  source_url, source_title, published_at, source_type, topic,"
                "  numeric_value, numeric_unit, confidence)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.id, self._run_id, "", ev.sub_question_id, ev.section, ev.claim,
                 ev.quote, ev.source_url, ev.source_title, ev.published_at,
                 ev.source_type, ev.topic, ev.numeric_value, ev.numeric_unit,
                 ev.confidence),
            )
            n += 1
        self.conn.commit()
        return n

    def finish_run(self, audit: dict) -> None:
        self.conn.execute(
            "UPDATE research_run SET status='completed', finished_at=?"
            " WHERE id=?",
            ("2026-01-01T00:00:00Z", self._run_id),
        )
        self.conn.execute(
            "INSERT INTO citation_audit"
            " (run_id, checked_at, citations, unresolved, orphans, coverage, passed)"
            " VALUES (?,?,?,?,?,?,?)",
            (self._run_id, "2026-01-01T00:00:00Z", audit["citations"],
             str(audit["unresolved"]), str(audit["orphans"]),
             audit["coverage"], int(audit["passed"])),
        )
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])
        return {
            "runs": one("SELECT COUNT(*) FROM research_run"),
            "documents": one("SELECT COUNT(*) FROM document"),
            "evidence": one("SELECT COUNT(*) FROM evidence"),
        }
