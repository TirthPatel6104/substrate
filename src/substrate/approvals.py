"""
Persistent approvals queue.

Replaces the blocking ``input()`` prompt from the original ``package_installer.py``:
a NEEDS_CONFIRMATION action becomes a durable ``pending`` record that a human
resolves out-of-band (CLI, the HTTP approvals page, or any MCP client). This is
what makes human-in-the-loop work inside an async agent loop or a headless
service.

Lifecycle:  pending -> approved -> executed
                    -> denied
                    -> expired   (via ``expire_stale``)
"""

from __future__ import annotations

import time

from .db import Database

_DEFAULT_TTL = 24 * 3600  # pending approvals older than a day are stale


class ApprovalQueue:
    def __init__(self, db: Database) -> None:
        self.db = db

    def request(self, command: str, reason: str, requested_by: str = "agent",
                cwd: str | None = None) -> int:
        cur = self.db.conn.execute(
            "INSERT INTO approvals(command, reason, requested_by, status, created_at, cwd) "
            "VALUES (?,?,?,?,?,?)",
            (command, reason, requested_by, "pending", time.time(), cwd),
        )
        self.db.conn.commit()
        return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, approval_id: int) -> dict | None:
        row = self.db.conn.execute(
            "SELECT * FROM approvals WHERE id=?", (approval_id,)
        ).fetchone()
        return dict(row) if row else None

    def decide(self, approval_id: int, approved: bool, decided_by: str = "user") -> bool:
        cur = self.db.conn.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=? "
            "WHERE id=? AND status='pending'",
            ("approved" if approved else "denied", decided_by, time.time(), approval_id),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def mark_executed(self, approval_id: int) -> None:
        """An approval is single-use: once run it cannot be run again."""
        self.db.conn.execute(
            "UPDATE approvals SET status='executed' WHERE id=? AND status='approved'",
            (approval_id,),
        )
        self.db.conn.commit()

    def expire_stale(self, ttl_seconds: float = _DEFAULT_TTL) -> int:
        cur = self.db.conn.execute(
            "UPDATE approvals SET status='expired' WHERE status='pending' AND created_at < ?",
            (time.time() - ttl_seconds,),
        )
        self.db.conn.commit()
        return cur.rowcount
