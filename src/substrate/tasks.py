"""
Task ledger: durable multi-step tasks that survive sessions and transfer between
agents. The key operation is ``resume``, which returns a compact "resume pack"
so any agent can pick up long work with full context.

Leases
------
Writes require a lease. ``claim`` grants one to an actor unless a *different*
actor holds a live lease, in which case ``LeaseHeld`` is raised and the caller
should either wait, ask for a handoff, or read-only ``resume``. Leases expire
(default 30 min) so a crashed agent cannot wedge a task forever. ``handoff``
releases the lease and records who it was handed to and why. Reads never need
a lease.
"""

from __future__ import annotations

import time

from .db import Database

_LEASE_SECONDS = 1800  # 30 minutes


class LeaseHeld(RuntimeError):
    def __init__(self, task_id: int, owner: str, until: float) -> None:
        self.task_id, self.owner, self.until = task_id, owner, until
        super().__init__(
            f"task {task_id} is leased to '{owner}' for another {int(until - time.time())}s; "
            "ask for tasks.handoff or wait for the lease to expire"
        )


class TaskLedger:
    def __init__(self, db: Database, *, lease_seconds: int = _LEASE_SECONDS) -> None:
        self.db = db
        self.lease_seconds = lease_seconds

    # -- create / mutate ------------------------------------------------------
    def create(self, goal: str, steps: list[str] | None = None, *, scope: str | None = None) -> int:
        goal = goal.strip()
        if not goal:
            raise ValueError("goal must not be empty")
        now = time.time()
        cur = self.db.conn.execute(
            "INSERT INTO tasks(goal, status, scope, created_at, updated_at) VALUES (?,?,?,?,?)",
            (goal, "open", scope, now, now),
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

    def add_step(self, task_id: int, description: str, *, actor: str | None = None) -> int:
        self._require_lease(task_id, actor)
        row = self.db.conn.execute(
            "SELECT COALESCE(MAX(ordinal), -1) + 1 AS nxt FROM task_steps WHERE task_id=?",
            (task_id,),
        ).fetchone()
        cur = self.db.conn.execute(
            "INSERT INTO task_steps(task_id, ordinal, description, status, updated_at) "
            "VALUES (?,?,?,?,?)",
            (task_id, int(row["nxt"]), description.strip(), "pending", time.time()),
        )
        self._touch(task_id)
        return int(cur.lastrowid)

    def update_step(self, step_id: int, *, status: str | None = None, notes: str | None = None,
                    actor: str | None = None) -> bool:
        valid = {"pending", "done", "blocked", "skipped"}
        row = self.db.conn.execute(
            "SELECT task_id FROM task_steps WHERE id=?", (step_id,)
        ).fetchone()
        if row is None:
            return False
        task_id = int(row["task_id"])
        self._require_lease(task_id, actor)
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
        cur = self.db.conn.execute(f"UPDATE task_steps SET {', '.join(sets)} WHERE id=?", params)
        if status == "done" or status == "skipped":
            self._maybe_autocomplete(task_id)
        elif status is not None:
            self.db.conn.execute(
                "UPDATE tasks SET status='in_progress', updated_at=? "
                "WHERE id=? AND status='open'", (time.time(), task_id))
        self._touch(task_id)
        return cur.rowcount > 0

    def set_status(self, task_id: int, status: str, *, actor: str | None = None) -> bool:
        valid = {"open", "in_progress", "blocked", "done", "abandoned"}
        if status not in valid:
            raise ValueError(f"Invalid task status '{status}'")
        self._require_lease(task_id, actor)
        cur = self.db.conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
            (status, time.time(), task_id),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    # -- leases -----------------------------------------------------------------
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
            (owner, now + self.lease_seconds, task_id),
        )
        self.db.conn.commit()
        return True

    def claim(self, task_id: int, owner: str) -> None:
        """Like ``acquire_lease`` but raises ``LeaseHeld`` so callers get the owner back."""
        if self.acquire_lease(task_id, owner):
            return
        row = self.db.conn.execute(
            "SELECT lease_owner, lease_until FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such task {task_id}")
        raise LeaseHeld(task_id, row["lease_owner"], float(row["lease_until"]))

    def release(self, task_id: int, owner: str | None) -> bool:
        cur = self.db.conn.execute(
            "UPDATE tasks SET lease_owner=NULL, lease_until=NULL, updated_at=? "
            "WHERE id=? AND (lease_owner=? OR ? IS NULL)",
            (time.time(), task_id, owner, owner),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def release_expired(self) -> int:
        cur = self.db.conn.execute(
            "UPDATE tasks SET lease_owner=NULL, lease_until=NULL "
            "WHERE lease_until IS NOT NULL AND lease_until < ?", (time.time(),),
        )
        self.db.conn.commit()
        return cur.rowcount

    def _require_lease(self, task_id: int, actor: str | None) -> None:
        """Writers must hold the lease; an actor with no competing holder gets it implicitly."""
        if actor is None:
            return  # library callers (tests, CLI) are trusted single-writers
        self.claim(task_id, actor)

    # -- read -----------------------------------------------------------------
    def progress(self, task_id: int) -> str:
        row = self.db.conn.execute(
            "SELECT COUNT(*) n, SUM(status IN ('done','skipped')) d FROM task_steps WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return f"{int(row['d'] or 0)}/{int(row['n'])} steps done"

    def resume(self, task_id: int) -> dict | None:
        """Return a compact resume pack for continuing the task."""
        t = self.db.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t is None:
            return None
        steps = self.db.conn.execute(
            "SELECT id AS step_id, ordinal, description, status, notes FROM task_steps "
            "WHERE task_id=? ORDER BY ordinal",
            (task_id,),
        ).fetchall()
        done = [dict(s) for s in steps if s["status"] in ("done", "skipped")]
        remaining = [dict(s) for s in steps if s["status"] in ("pending", "blocked")]
        blockers = [dict(s) for s in steps if s["status"] == "blocked"]
        next_step = next((dict(s) for s in steps if s["status"] == "pending"), None)
        handoffs = [
            dict(r) for r in self.db.conn.execute(
                "SELECT ts, actor, args FROM audit WHERE tool='tasks.handoff' AND args LIKE ? "
                "ORDER BY ts DESC LIMIT 5", (f'%"task_id": {task_id},%',))
        ]
        live = t["lease_owner"] if (t["lease_until"] or 0) > time.time() else None
        return {
            "task_id": task_id,
            "goal": t["goal"],
            "status": t["status"],
            "scope": t["scope"],
            "lease_owner": live,
            "progress": f"{len(done)}/{len(steps)} steps done",
            "next_step": next_step,
            "completed_steps": done,
            "remaining_steps": remaining,
            "blockers": blockers,
            "notes": [s["notes"] for s in steps if s["notes"]],
            "recent_handoffs": handoffs,
        }

    def handoff(self, task_id: int, to_hint: str | None = None, *, actor: str | None = None,
                note: str | None = None) -> dict | None:
        """Release the lease and produce a handoff document for another agent."""
        if self.db.conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
            return None
        self.db.conn.execute(
            "UPDATE tasks SET lease_owner=NULL, lease_until=NULL, updated_at=? WHERE id=?",
            (time.time(), task_id),
        )
        self.db.conn.commit()
        self.db.audit(actor or "agent", "tasks.handoff",
                      {"task_id": task_id, "to": to_hint, "note": note}, "-", "released")
        pack = self.resume(task_id)
        if pack is not None:
            pack["handoff_to"] = to_hint
            pack["handoff_note"] = note
            pack["handoff_from"] = actor
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

    # -- internals ------------------------------------------------------------
    def _maybe_autocomplete(self, task_id: int) -> None:
        rows = self.db.conn.execute(
            "SELECT status FROM task_steps WHERE task_id=?", (task_id,)
        ).fetchall()
        if rows and all(r["status"] in ("done", "skipped") for r in rows):
            self.db.conn.execute(
                "UPDATE tasks SET status='done', lease_owner=NULL, lease_until=NULL, "
                "updated_at=? WHERE id=?",
                (time.time(), task_id),
            )

    def _touch(self, task_id: int) -> None:
        self.db.conn.execute(
            "UPDATE tasks SET updated_at=? WHERE id=?", (time.time(), task_id)
        )
        self.db.conn.commit()
