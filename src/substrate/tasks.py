"""
Task ledger: durable multi-step tasks that survive sessions and transfer between
agents. The key operation is ``resume``, which returns a compact "resume pack"
so any agent can pick up long work with full context.

Write access uses a lease so two agents don't clobber the same task; reads are
always allowed.
"""

from __future__ import annotations

import time

from .db import Database

_LEASE_SECONDS = 1800  # 30 minutes


class TaskLedger:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, goal: str, steps: list[str] | None = None, *, scope: str | None = None) -> int:
        now = time.time()
        cur = self.db.conn.execute(
            "INSERT INTO tasks(goal, status, scope, created_at, updated_at) VALUES (?,?,?,?,?)",
            (goal.strip(), "open", scope, now, now),
        )
        task_id = int(cur.lastrowid)
        for i, desc in enumerate(steps or []):
            self.db.conn.execute(
                "INSERT INTO task_steps(task_id, ordinal, description, status, updated_at) "
                "VALUES (?,?,?,?,?)",
                (task_id, i, desc.strip(), "pending", now),
            )
        self.db.conn.commit()
        return task_id

    def add_step(self, task_id: int, description: str) -> int:
        row = self.db.conn.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 AS nxt FROM task_steps WHERE task_id=?",
            (task_id,),
        ).fetchone()
        ordinal = int(row["nxt"])
        cur = self.db.conn.execute(
            "INSERT INTO task_steps(task_id, ordinal, description, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, ordinal, description.strip(), "pending", time.time()),
        )
        self._touch(task_id)
        return int(cur.lastrowid)

    def update_step(self, step_id: int, *, status: str | None = None, notes: str | None = None) -> bool:
        valid = {"pending", "done", "blocked", "skipped"}
        sets, params = [], []
        if status is not None:
            if status not in valid:
                raise ValueError(f"Invalid step status '{status}'")
            sets.append("status=?")
            params.append(status)
        if notes is not None:
            sets.append("notes=?")
            params.append(notes)
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(step_id)
        cur = self.db.conn.execute(
            f"UPDATE task_steps SET {', '.join(sets)} WHERE id=?", params
        )
        row = self.db.conn.execute(
            "SELECT task_id FROM task_steps WHERE id=?", (step_id,)
        ).fetchone()
        if row:
            self._maybe_autocomplete(int(row["task_id"]))
        self.db.conn.commit()
        return cur.rowcount > 0

    def set_status(self, task_id: int, status: str) -> bool:
        valid = {"open", "in_progress", "blocked", "done", "abandoned"}
        if status not in valid:
            raise ValueError(f"Invalid task status '{status}'")
        cur = self.db.conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), task_id),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def acquire_lease(self, task_id: int, owner: str) -> bool:
        """Grant a write lease to ``owner`` unless another owner holds a live one."""
        now = time.time()
        row = self.db.conn.execute(
            "SELECT lease_owner, lease_until FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            return False
        if row["lease_owner"] and row["lease_owner"] != owner and (row["lease_until"] or 0) > now:
            return False
        self.db.conn.execute(
            "UPDATE tasks SET lease_owner=?, lease_until=? WHERE id=?",
            (owner, now + _LEASE_SECONDS, task_id),
        )
        self.db.conn.commit()
        return True

    def resume(self, task_id: int) -> dict | None:
        """Return a compact resume pack for continuing the task."""
        t = self.db.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t is None:
            return None
        steps = self.db.conn.execute(
            "SELECT ordinal, description, status, notes FROM task_steps "
            "WHERE task_id=? ORDER BY ordinal",
            (task_id,),
        ).fetchall()
        done = [dict(s) for s in steps if s["status"] == "done"]
        remaining = [dict(s) for s in steps if s["status"] in ("pending", "blocked")]
        blockers = [dict(s) for s in steps if s["status"] == "blocked"]
        return {
            "task_id": task_id,
            "goal": t["goal"],
            "status": t["status"],
            "scope": t["scope"],
            "progress": f"{len(done)}/{len(steps)} steps done",
            "completed_steps": done,
            "remaining_steps": remaining,
            "blockers": blockers,
            "notes": [s["notes"] for s in steps if s["notes"]],
        }

    def handoff(self, task_id: int, to_hint: str | None = None) -> dict | None:
        """Freeze the lease and produce a handoff document for another agent."""
        self.db.conn.execute(
            "UPDATE tasks SET lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.db.conn.commit()
        pack = self.resume(task_id)
        if pack is not None:
            pack["handoff_to"] = to_hint
        return pack

    def list_open(self, *, scope: str | None = None) -> list[dict]:
        if scope:
            rows = self.db.conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('done','abandoned') "
                "AND (scope=? OR scope IS NULL) ORDER BY updated_at DESC",
                (scope,),
            ).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('done','abandoned') "
                "ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def _maybe_autocomplete(self, task_id: int) -> None:
        rows = self.db.conn.execute(
            "SELECT status FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchall()
        if rows and all(r["status"] in ("done", "skipped") for r in rows):
            self.db.conn.execute(
                "UPDATE tasks SET status='done', updated_at=? WHERE id=?",
                (time.time(), task_id),
            )

    def _touch(self, task_id: int) -> None:
        self.db.conn.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?", (time.time(), task_id)
        )
        self.db.conn.commit()
