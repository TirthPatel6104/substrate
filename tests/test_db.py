"""Storage tests: schema versioning and forward-only migrations."""

import sqlite3

from substrate.db import SCHEMA_VERSION, Database


def test_fresh_database_is_stamped_with_latest_version(tmp_path):
    db = Database(tmp_path / "fresh.db")
    assert db.schema_version == SCHEMA_VERSION
    db.close()


def test_v1_database_migrates_forward(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('schema_version','1');
        CREATE TABLE approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, reason TEXT,
            requested_by TEXT, status TEXT NOT NULL DEFAULT 'pending', decided_by TEXT,
            created_at REAL NOT NULL, decided_at REAL
        );
        INSERT INTO approvals(command, created_at) VALUES ('pip install x', 1.0);
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(approvals)")}
    assert "cwd" in cols
    assert db.schema_version == SCHEMA_VERSION
    # Existing rows survive the migration.
    assert db.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    db.close()

    # Re-opening is idempotent.
    db2 = Database(path)
    assert db2.schema_version == SCHEMA_VERSION
    db2.close()


def test_memory_database_works():
    db = Database(":memory:")
    db.audit("t", "x", {"a": 1}, "SAFE", "ok")
    assert db.conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    db.close()
