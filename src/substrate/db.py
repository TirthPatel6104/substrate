"""
SQLite storage for Substrate.

One database file per workspace (plus a global DB for cross-workspace memory).
Uses WAL mode for concurrent readers, FTS5 for lexical search, and plain tables
for vectors and graph edges. No external database server is required.

Vectors are stored as JSON arrays. This is deliberately simple for the MVP; the
similarity search is done in Python. For large corpora, swap the vector columns
for ``sqlite-vec`` without changing the public API of the store modules.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ── Memory ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL,          -- working|episodic|semantic|task|project
    scope       TEXT NOT NULL,          -- global|<workspace>|task:<id>
    content     TEXT NOT NULL,
    source      TEXT,                   -- provenance: which agent/session
    confidence  REAL DEFAULT 1.0,
    importance  REAL DEFAULT 1.0,
    uses        INTEGER DEFAULT 0,
    pinned      INTEGER DEFAULT 0,
    superseded_by INTEGER,              -- id of the memory that replaced this
    embedding   TEXT,                   -- JSON float array
    emb_model   TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    expires_at  REAL                    -- for working memory TTL
);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, type);
CREATE INDEX IF NOT EXISTS idx_mem_active ON memories(superseded_by);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ── Files ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    hash        TEXT,
    size        INTEGER,
    language    TEXT,
    mtime       REAL,
    indexed_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(hash);

CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    embedding   TEXT,
    emb_model   TEXT
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content, content='chunks', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

-- ── Knowledge graph ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    src   TEXT NOT NULL,   -- e.g. file:12, task:3, memory:9
    dst   TEXT NOT NULL,
    type  TEXT NOT NULL,   -- imports|co_changed_with|similar_to|touched_by_task|supersedes
    weight REAL DEFAULT 1.0,
    PRIMARY KEY (src, dst, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);

-- ── Tasks ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open|in_progress|blocked|done|abandoned
    scope       TEXT,
    lease_owner TEXT,                            -- agent currently holding write lease
    lease_until REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS task_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|done|blocked|skipped
    notes       TEXT,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON task_steps(task_id);

-- ── Approvals queue ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    reason      TEXT,
    requested_by TEXT,
    status      TEXT NOT NULL DEFAULT 'pending', -- pending|approved|denied|expired
    decided_by  TEXT,
    created_at  REAL NOT NULL,
    decided_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_appr_status ON approvals(status);

-- ── Audit log (append-only) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT,
    tool        TEXT NOT NULL,
    args        TEXT,
    verdict     TEXT,
    result      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
"""


class Database:
    """Thin wrapper over a SQLite connection with the Substrate schema applied."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        self.conn.commit()

    # -- helpers --------------------------------------------------------------
    def audit(self, actor: str, tool: str, args: object, verdict: str, result: str) -> None:
        self.conn.execute(
            "INSERT INTO audit(ts, actor, tool, args, verdict, result) VALUES (?,?,?,?,?,?)",
            (time.time(), actor, tool, json.dumps(args, default=str)[:4000], verdict, result[:2000]),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
