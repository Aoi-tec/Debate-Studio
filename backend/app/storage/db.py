"""SQLite persistence (DESIGN 4.5 / 4.6.2).

A single connection guarded by an RLock. Statements are short, so they are executed
synchronously from the event loop. Secrets are never written here.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS debate_sessions (
  id TEXT PRIMARY KEY, topic TEXT NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, started_at TEXT, ended_at TEXT,
  snapshot_json TEXT NOT NULL, progress_json TEXT NOT NULL DEFAULT '{}',
  final_report_json TEXT, conclusion_status TEXT, error TEXT,
  blockers_json TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT, seq INTEGER NOT NULL,
  round INTEGER, phase TEXT, kind TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, tier TEXT, profile_id TEXT, model_id TEXT,
  scope_json TEXT NOT NULL DEFAULT '[]', packet_refs_json TEXT NOT NULL DEFAULT '[]',
  handoff_id TEXT, created_at TEXT NOT NULL, completed_at TEXT, meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, seq);

CREATE TABLE IF NOT EXISTS stream_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL,
  payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_session ON stream_events(session_id, id);

CREATE TABLE IF NOT EXISTS usage_records (
  id TEXT PRIMARY KEY, session_id TEXT, agent_id TEXT, request_id TEXT, attempt INTEGER,
  purpose TEXT, profile_id TEXT, pool_id TEXT, tier TEXT, model_id TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER, reasoning_tokens INTEGER,
  usage_source TEXT NOT NULL, cost REAL, cost_status TEXT NOT NULL DEFAULT 'uncalculated',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_session ON usage_records(session_id);
CREATE INDEX IF NOT EXISTS ix_usage_pool ON usage_records(pool_id, created_at);

CREATE TABLE IF NOT EXISTS usage_baselines (
  id TEXT PRIMARY KEY, session_id TEXT, pool_id TEXT NOT NULL, profile_id TEXT,
  family TEXT, period TEXT, u0 INTEGER, limit_tokens INTEGER, source TEXT NOT NULL,
  fetched_at TEXT NOT NULL, estimated INTEGER NOT NULL DEFAULT 0, detail TEXT
);

CREATE TABLE IF NOT EXISTS agent_budgets (
  session_id TEXT NOT NULL, agent_id TEXT NOT NULL, tier TEXT NOT NULL,
  used_tokens INTEGER NOT NULL DEFAULT 0, limit_tokens INTEGER,
  warned INTEGER NOT NULL DEFAULT 0, last_switch_at TEXT,
  PRIMARY KEY (session_id, agent_id, tier)
);

CREATE TABLE IF NOT EXISTS model_handoffs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  from_tier TEXT, to_tier TEXT, from_model TEXT, to_model TEXT, reason TEXT,
  partial_message_id TEXT, new_message_id TEXT, summary_id TEXT, quality TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debate_checkpoints (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, round INTEGER, report_json TEXT,
  decision TEXT, instruction TEXT, decided_by TEXT, decided_at TEXT,
  idempotency_key TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS uploaded_files (
  file_id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
  sha256 TEXT NOT NULL, mime TEXT, kind TEXT NOT NULL, size INTEGER NOT NULL,
  created_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
  extract_version INTEGER NOT NULL DEFAULT 0, deleted_at TEXT, stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS file_chunks (
  chunk_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, extract_version INTEGER NOT NULL,
  seq INTEGER NOT NULL, text TEXT NOT NULL, tokens INTEGER NOT NULL,
  location_json TEXT NOT NULL, quality TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_file ON file_chunks(file_id, extract_version, seq);

CREATE TABLE IF NOT EXISTS file_processing_jobs (
  job_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0, started_at TEXT, ended_at TEXT, error_code TEXT
);

CREATE TABLE IF NOT EXISTS session_attachments (
  session_id TEXT NOT NULL, file_id TEXT NOT NULL, message_id TEXT, attached_at TEXT NOT NULL,
  allowed_agent_ids_json TEXT, enabled INTEGER NOT NULL DEFAULT 1, apply_from_seq INTEGER,
  attach_version INTEGER NOT NULL DEFAULT 1, required INTEGER NOT NULL DEFAULT 0,
  allow_partial INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, file_id)
);

CREATE TABLE IF NOT EXISTS chair_analysis_runs (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, chair_agent_id TEXT, purpose TEXT,
  file_ids_json TEXT, file_versions_json TEXT, settings_json TEXT, status TEXT NOT NULL,
  chunk_ids_json TEXT, extraction_success_rate REAL, model_id TEXT, tier TEXT,
  usage_json TEXT, created_at TEXT NOT NULL, ended_at TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS chair_evidence (
  evidence_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, file_id TEXT, file_version INTEGER,
  chunk_id TEXT, location_json TEXT, source_label TEXT, quote TEXT, quality TEXT,
  allowed_agents_json TEXT, status TEXT NOT NULL DEFAULT 'valid', created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_packets (
  packet_id TEXT NOT NULL, packet_version INTEGER NOT NULL, session_id TEXT NOT NULL,
  recipient_agent_id TEXT NOT NULL, body_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
  source_file_versions_json TEXT NOT NULL, analysis_run_id TEXT, token_estimate INTEGER,
  scope_files_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY (packet_id, packet_version)
);
CREATE INDEX IF NOT EXISTS ix_packets_recipient ON context_packets(session_id, recipient_agent_id, status);

CREATE TABLE IF NOT EXISTS context_packet_deliveries (
  id TEXT PRIMARY KEY, packet_id TEXT NOT NULL, packet_version INTEGER NOT NULL,
  recipient_agent_id TEXT NOT NULL, acl_json TEXT, request_id TEXT, hash TEXT, delivered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_summaries (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, version INTEGER NOT NULL,
  from_seq INTEGER, to_seq INTEGER, summary TEXT NOT NULL, usage_json TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS context_requests (
  request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, agent_id TEXT NOT NULL,
  turn_message_id TEXT, body_json TEXT NOT NULL, status TEXT NOT NULL,
  dedupe_key TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_requests (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, origin_context_request_id TEXT,
  agent_id TEXT, turn_message_id TEXT, body_json TEXT NOT NULL, state TEXT NOT NULL,
  reason_code TEXT, criticality TEXT, version INTEGER NOT NULL DEFAULT 1,
  ask_count INTEGER NOT NULL DEFAULT 1, packet_id TEXT, packet_version INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_responses (
  id TEXT PRIMARY KEY, evidence_request_id TEXT NOT NULL, action TEXT NOT NULL,
  statement TEXT, file_ids_json TEXT, source_kind TEXT, verification_status TEXT,
  idempotency_key TEXT NOT NULL UNIQUE, result_json TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_resolution_events (
  id TEXT PRIMARY KEY, evidence_request_id TEXT NOT NULL, from_state TEXT, to_state TEXT,
  actor TEXT, search_run_id TEXT, packet_id TEXT, idempotency_key TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provenance_items (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source_kind TEXT NOT NULL,
  verification_status TEXT NOT NULL, text TEXT NOT NULL, evidence_request_id TEXT,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]', criticality TEXT, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT, action TEXT NOT NULL,
  detail_json TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.fts_mode = "none"
        self.migrate()

    # ------------------------------------------------------------ schema
    def migrate(self) -> None:
        with self._lock:
            self.conn.executescript(_SCHEMA)
            row = self.conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            self._init_fts()

    def _init_fts(self) -> None:
        exists = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='file_chunk_fts'"
        ).fetchone()
        if exists:
            self.fts_mode = "trigram" if "trigram" in (exists[0] or "") else "unicode61"
            return
        try:
            self.conn.execute(
                "CREATE VIRTUAL TABLE file_chunk_fts USING fts5(text, chunk_id UNINDEXED, file_id UNINDEXED, tokenize='trigram')"
            )
            self.fts_mode = "trigram"
        except sqlite3.OperationalError:
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE file_chunk_fts USING fts5(text, chunk_id UNINDEXED, file_id UNINDEXED)"
                )
                self.fts_mode = "unicode61"
            except sqlite3.OperationalError:
                self.fts_mode = "none"

    # ------------------------------------------------------------ helpers
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, tuple(params))

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self.conn.executemany(sql, [tuple(p) for p in seq])

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            r = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def tx(self):
        return _Tx(self)

    def insert(self, table: str, row: dict) -> None:
        cols = ",".join(row.keys())
        q = ",".join("?" for _ in row)
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({q})", [_enc(v) for v in row.values()])

    def update(self, table: str, key: dict, values: dict) -> int:
        sets = ",".join(f"{k}=?" for k in values)
        where = " AND ".join(f"{k}=?" for k in key)
        cur = self.execute(
            f"UPDATE {table} SET {sets} WHERE {where}",
            [_enc(v) for v in values.values()] + list(key.values()),
        )
        return cur.rowcount

    def audit(self, action: str, detail: dict | None = None, actor: str = "user") -> None:
        from app.core.ids import now_iso
        from app.core.redact import redact

        self.execute(
            "INSERT INTO audit_log(ts, actor, action, detail_json) VALUES (?,?,?,?)",
            (now_iso(), actor, action, redact(json.dumps(detail or {}, ensure_ascii=False))),
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self) -> Database:
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.db.conn.execute("COMMIT")
            else:
                self.db.conn.execute("ROLLBACK")
        finally:
            self.db._lock.release()


def _enc(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return int(v)
    return v


def dec(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default
