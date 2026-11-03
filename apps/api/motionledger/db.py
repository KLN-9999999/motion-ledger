from __future__ import annotations
import json, sqlite3
from pathlib import Path
from typing import Any
from contextlib import contextmanager

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS procedures (
  id TEXT NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
  body_json TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(id, version)
);
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, original_name TEXT NOT NULL, original_path TEXT NOT NULL,
  proxy_path TEXT NOT NULL, sha256 TEXT NOT NULL, duration_s REAL NOT NULL,
  fps REAL NOT NULL, frames INTEGER NOT NULL, width INTEGER NOT NULL,
  height INTEGER NOT NULL, file_id TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  id TEXT PRIMARY KEY, video_id TEXT NOT NULL, procedure_id TEXT NOT NULL,
  procedure_version INTEGER NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(video_id) REFERENCES videos(id)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, audit_id TEXT NOT NULL, version INTEGER NOT NULL,
  parent_id TEXT, mode TEXT NOT NULL, status TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL,
  completed_at TEXT, cost_usd REAL NOT NULL DEFAULT 0,
  report_json TEXT, error TEXT,
  UNIQUE(audit_id, version), FOREIGN KEY(audit_id) REFERENCES audits(id)
);
CREATE TABLE IF NOT EXISTS overrides (
  id TEXT PRIMARY KEY, audit_id TEXT NOT NULL, run_id TEXT NOT NULL,
  kind TEXT NOT NULL, instance_id TEXT, payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
  id TEXT PRIMARY KEY, audit_id TEXT NOT NULL, seq INTEGER NOT NULL,
  kind TEXT NOT NULL, payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL, at TEXT NOT NULL,
  UNIQUE(audit_id, seq)
);
CREATE TABLE IF NOT EXISTS model_calls (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, model TEXT NOT NULL,
  endpoint TEXT NOT NULL, request_sha256 TEXT NOT NULL,
  response_id TEXT, usage_json TEXT, latency_ms INTEGER,
  cost_usd REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  cache_key TEXT PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL,
  metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback(); raise
        finally:
            conn.close()

    @staticmethod
    def decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as c: c.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.connect() as c: return self.decode(c.execute(sql, params).fetchone())

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as c: return [dict(r) for r in c.execute(sql, params).fetchall()]

    def insert_json(self, table: str, fields: dict[str, Any]) -> None:
        cols = list(fields)
        values = [json.dumps(v, separators=(",", ":")) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self.connect() as c:
            c.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", values)

    def update(self, table: str, key_field: str, key: str, fields: dict[str, Any]) -> None:
        assignments=[]; values=[]
        for k,v in fields.items():
            assignments.append(f"{k}=?")
            values.append(json.dumps(v, separators=(",", ":")) if isinstance(v,(dict,list)) else v)
        values.append(key)
        with self.connect() as c: c.execute(f"UPDATE {table} SET {','.join(assignments)} WHERE {key_field}=?", values)
