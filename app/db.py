"""SQLite persistence layer. A fresh connection is opened per operation,
which keeps the background validation worker thread-safe."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS imports (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    doc_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id   TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    UNIQUE (import_id, path)
);

CREATE TABLE IF NOT EXISTS validations (
    id          TEXT PRIMARY KEY,
    import_id   TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,               -- pending | running | done | failed
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    summary     TEXT,                        -- JSON
    error       TEXT
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    validation_id TEXT NOT NULL REFERENCES validations(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    severity      TEXT NOT NULL,
    path          TEXT NOT NULL,
    line          INTEGER,
    message       TEXT NOT NULL,
    details       TEXT NOT NULL DEFAULT '{}'  -- JSON
);

CREATE INDEX IF NOT EXISTS idx_documents_import ON documents(import_id);
CREATE INDEX IF NOT EXISTS idx_validations_import ON validations(import_id);
CREATE INDEX IF NOT EXISTS idx_diagnostics_validation ON diagnostics(validation_id);
"""


def _db_path() -> str:
    return os.environ.get("DB_PATH", _DEFAULT_DB)


def _conn() -> sqlite3.Connection:
    path = _db_path()
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    conn = _conn()
    try:
        with conn:
            conn.executescript(SCHEMA)
    finally:
        conn.close()


# ---------------------------------------------------------------- imports ---

def create_import(documents: dict[str, str]) -> str:
    import_id = uuid.uuid4().hex
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO imports (id, created_at, doc_count) VALUES (?, ?, ?)",
                (import_id, _now(), len(documents)),
            )
            conn.executemany(
                "INSERT INTO documents (import_id, path, content) VALUES (?, ?, ?)",
                [(import_id, p, c) for p, c in documents.items()],
            )
    finally:
        conn.close()
    return import_id


def get_import(import_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_document_paths(import_id: str) -> list[str]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT path FROM documents WHERE import_id = ? ORDER BY path", (import_id,)
        ).fetchall()
        return [r["path"] for r in rows]
    finally:
        conn.close()


def get_documents(import_id: str) -> dict[str, str]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT path, content FROM documents WHERE import_id = ? ORDER BY path",
            (import_id,),
        ).fetchall()
        return {r["path"]: r["content"] for r in rows}
    finally:
        conn.close()


def get_document(import_id: str, path: str) -> str | None:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT content FROM documents WHERE import_id = ? AND path = ?",
            (import_id, path),
        ).fetchone()
        return row["content"] if row else None
    finally:
        conn.close()


# ------------------------------------------------------------ validations ---

def create_validation(import_id: str) -> str:
    validation_id = uuid.uuid4().hex
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO validations (id, import_id, status, created_at) VALUES (?, ?, 'pending', ?)",
                (validation_id, import_id, _now()),
            )
    finally:
        conn.close()
    return validation_id


def set_validation_running(validation_id: str) -> None:
    conn = _conn()
    try:
        with conn:
            conn.execute("UPDATE validations SET status = 'running' WHERE id = ?", (validation_id,))
    finally:
        conn.close()


def finish_validation(validation_id: str, status: str, summary: dict | None = None, error: str | None = None) -> None:
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "UPDATE validations SET status = ?, finished_at = ?, summary = ?, error = ? WHERE id = ?",
                (status, _now(), json.dumps(summary, ensure_ascii=False) if summary else None, error, validation_id),
            )
    finally:
        conn.close()


def insert_diagnostics(validation_id: str, diagnostics: list[dict]) -> None:
    conn = _conn()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO diagnostics (validation_id, type, severity, path, line, message, details)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        validation_id,
                        d["type"],
                        d["severity"],
                        d["path"],
                        d["line"],
                        d["message"],
                        json.dumps(d["details"], ensure_ascii=False),
                    )
                    for d in diagnostics
                ],
            )
    finally:
        conn.close()


def get_validation(validation_id: str) -> dict | None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM validations WHERE id = ?", (validation_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["summary"] = json.loads(out["summary"]) if out["summary"] else None
        return out
    finally:
        conn.close()


def list_validations(limit: int = 50) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM validations ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"]) if d["summary"] else None
            out.append(d)
        return out
    finally:
        conn.close()


def list_diagnostics(validation_id: str, type_: str | None = None, path: str | None = None) -> list[dict]:
    sql = "SELECT * FROM diagnostics WHERE validation_id = ?"
    params: list = [validation_id]
    if type_:
        sql += " AND type = ?"
        params.append(type_)
    if path:
        sql += " AND path = ?"
        params.append(path)
    sql += " ORDER BY path, line, id"
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["details"] = json.loads(d["details"])
            out.append(d)
        return out
    finally:
        conn.close()
