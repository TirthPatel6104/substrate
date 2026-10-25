"""
Substrate core: wires the database and all subsystems together behind one
object, and exposes a single ``dispatch(tool, params, actor=...)`` entry point
used by the MCP servers (stdio + HTTP) and the CLI so they can never drift apart.

Concurrency model
-----------------
One ``Substrate`` instance may be shared by several threads (the HTTP daemon
serves one agent per connection). SQLite is a single-writer store, so every
dispatch holds a process-wide re-entrant lock: tool calls are serialised, which
is both correct and plenty fast for a per-machine daemon (calls are ~ms).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .approvals import ApprovalQueue
from .config import db_path_for
from .db import Database
from .executor import SafeExecutor
from .files import FileIndex
from .memory import MemoryStore, SecretRefused
from .tasks import LeaseHeld, TaskLedger


class Substrate:
    def __init__(self, workspace: str = "global", db_path: str | Path | None = None) -> None:
        self.workspace = workspace
        self.db = Database(db_path or db_path_for(workspace))
        self.memory = MemoryStore(self.db)
        self.files = FileIndex(self.db)
        self.tasks = TaskLedger(self.db)
        self.approvals = ApprovalQueue(self.db)
        self.executor = SafeExecutor(self.db)
        self._lock = threading.RLock()
        self.started_at = time.time()

    def close(self) -> None:
        self.db.close()

    # -- composite views --------------------------------------------------------
    def brief(self, scope: str | None = None) -> dict:
        """Session-start context pack: memory brief + open tasks + pending approvals."""
        scope = scope or self.workspace
        pack = self.memory.brief(scope)
        pack["open_tasks"] = [
            {"task_id": t["id"], "goal": t["goal"], "status": t["status"],
             "lease_owner": t["lease_owner"], "progress": self.tasks.progress(int(t["id"]))}
            for t in self.tasks.list_open(scope=scope)
        ]
        pack["pending_approvals"] = len(self.approvals.pending())
        return pack

    def brief_markdown(self, scope: str | None = None) -> str:
        """Render the brief as CLAUDE.md-style markdown for file-only agents."""
        b = self.brief(scope)
        lines = [f"# Project brief: {b['scope']}", ""]
        for title, key in [
            ("Project profile", "project"),
            ("Pinned facts", "pinned_facts"),
            ("Known facts & decisions", "semantic"),
            ("Recent activity", "recent_episodes"),
        ]:
            items = b[key]
            if items:
                lines.append(f"## {title}")
                for it in items:
                    lines.append(f"- {it['content']}")
                lines.append("")
        if b["open_tasks"]:
            lines.append("## Open tasks")
            for t in b["open_tasks"]:
                owner = f" (held by {t['lease_owner']})" if t["lease_owner"] else ""
                lines.append(f"- [{t['task_id']}] {t['goal']} — {t['progress']}{owner}")
            lines.append("")
        if b["pending_approvals"]:
            lines.append(f"## Pending approvals: {b['pending_approvals']} "
                         "(run `substrate approvals`)")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def maintenance(self, scope: str | None = None) -> dict:
        """Housekeeping: expire TTLs, compact working memory, decay importance, expire approvals."""
        scope = scope or self.workspace
        compacted = self.memory.compact_working(scope)
        return {
            "expired_working": self.memory.expire_working(),
            "compacted_into": compacted,
            "decayed": self.memory.decay(),
            "reembedded_memories": self.memory.reembed_stale(),
            "expired_approvals": self.approvals.expire_stale(),
            "released_leases": self.tasks.release_expired(),
        }

    def session_end(self, summary: str, *, scope: str | None = None, actor: str = "agent",
                    task_id: int | None = None) -> dict:
        """Record what a session did as an episodic memory and tidy up working state."""
        scope = scope or self.workspace
        summary = summary.strip()
        if not summary:
            raise ValueError("summary must not be empty")
        mid = self.memory.remember(
            f"Session ({actor}): {summary}", mem_type="episodic", scope=scope, source=actor
        )
        if task_id is not None:
            self.tasks.release(int(task_id), actor)
        compacted = self.memory.compact_working(scope, older_than_seconds=0)
        return {"episode_id": mid, "compacted_into": compacted}

    # -- unified tool dispatch ------------------------------------------------
    def dispatch(self, tool: str, params: dict | None, *, actor: str = "agent") -> dict:
        """Route an MCP-style tool call to the right subsystem (thread-safe)."""
        with self._lock:
            try:
                return self._dispatch(tool, params or {}, actor)
            except SecretRefused as e:
                return {"error": str(e), "code": "secret_refused"}
            except LeaseHeld as e:
                return {"error": str(e), "code": "lease_held", "lease_owner": e.owner}
            except KeyError as e:
                return {"error": f"missing required parameter: {e}", "code": "bad_request"}
            except (ValueError, TypeError) as e:
                return {"error": str(e), "code": "bad_request"}

    def _dispatch(self, tool: str, p: dict, actor: str) -> dict:  # noqa: C901 - flat router
        scope = p.get("scope") or self.workspace

        # memory.*
        if tool == "memory.remember":
            mid = self.memory.remember(
                p["content"],
                mem_type=p.get("type", "semantic"),
                scope=scope,
                source=p.get("source", actor),
                importance=float(p.get("importance", 1.0)),
                pinned=bool(p.get("pinned", False)),
                ttl_seconds=p.get("ttl_seconds"),
            )
            return {"id": mid}
        if tool == "memory.recall":
            return {"results": self.memory.recall(p["query"], scope=scope, k=int(p.get("k", 5)))}
        if tool == "memory.brief":
            if p.get("format") == "markdown":
                return {"markdown": self.brief_markdown(scope)}
            return self.brief(scope)
        if tool == "memory.forget":
            return {"forgotten": self.memory.forget(int(p["id"]))}
        if tool == "memory.supersede":
            return {"id": self.memory.supersede(int(p["id"]), p["content"], source=actor)}
        if tool == "memory.list":
            return {"memories": self.memory.list(scope=scope, mem_type=p.get("type"),
                                                 limit=int(p.get("limit", 50)))}

        # files.*
        if tool == "files.index":
            return self.files.index_path(p["path"])
        if tool == "files.search":
            return {"results": self.files.search(p["query"], k=int(p.get("k", 8)))}
        if tool == "files.similar":
            return {"results": self.files.similar_files(p["path"], k=int(p.get("k", 5)))}
        if tool == "files.stats":
            return self.files.stats()

        # tasks.*
        if tool == "tasks.create":
            tid = self.tasks.create(p["goal"], p.get("steps"), scope=scope)
            self.tasks.acquire_lease(tid, actor)
            return {"task_id": tid, "lease_owner": actor}
        if tool == "tasks.claim":
            tid = int(p["task_id"])
            self.tasks.claim(tid, actor)
            return self.tasks.resume(tid) or {"error": "no such task"}
        if tool == "tasks.release":
            return {"released": self.tasks.release(int(p["task_id"]), actor)}
        if tool == "tasks.add_step":
            return {"step_id": self.tasks.add_step(int(p["task_id"]), p["description"], actor=actor)}
        if tool == "tasks.update_step":
            ok = self.tasks.update_step(
                int(p["step_id"]), status=p.get("status"), notes=p.get("notes"), actor=actor
            )
            return {"updated": ok}
        if tool == "tasks.set_status":
            return {"updated": self.tasks.set_status(int(p["task_id"]), p["status"], actor=actor)}
        if tool == "tasks.resume":
            pack = self.tasks.resume(int(p["task_id"]))
            return pack or {"error": "no such task"}
        if tool == "tasks.handoff":
            pack = self.tasks.handoff(int(p["task_id"]), p.get("to"), actor=actor,
                                      note=p.get("note"))
            return pack or {"error": "no such task"}
        if tool == "tasks.list":
            return {"tasks": self.tasks.list_open(scope=scope)}

        # exec.* / approvals.*
        if tool == "exec.propose":
            return self.executor.propose(
                p["command"], actor=actor, cwd=p.get("cwd"), timeout=p.get("timeout")
            ).to_dict()
        if tool == "exec.classify":
            from .safety import safety_level
            cls = safety_level(p["command"])
            return {"verdict": str(cls.level), "reason": cls.reason}
        if tool == "exec.run_approved":
            return self.executor.run_approved(int(p["approval_id"]), actor=actor).to_dict()
        if tool == "approvals.pending":
            return {"pending": self.approvals.pending()}
        if tool == "approvals.decide":
            ok = self.approvals.decide(int(p["id"]), bool(p["approved"]), decided_by=actor)
            return {"decided": ok}

        # session.* / maintenance
        if tool == "session.start":
            return self.brief(scope) | {"actor": actor}
        if tool == "session.end":
            return self.session_end(p["summary"], scope=scope, actor=actor,
                                    task_id=p.get("task_id"))
        if tool == "maintenance.run":
            return self.maintenance(scope)

        return {"error": f"unknown tool '{tool}'", "code": "unknown_tool"}


def _s(desc: str, required: list[str] | None = None, **props) -> dict:
    schema: dict = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


_STR = {"type": "string"}
_INT = {"type": "integer"}
_SCOPE = {"type": "string", "description": "workspace scope; defaults to the server's workspace"}
_MEM_TYPES = {"type": "string", "enum": ["working", "episodic", "semantic", "task", "project"]}

# Public catalog of tools, used to advertise capabilities over MCP.
TOOL_SCHEMAS: list[dict] = [
    {"name": "memory.remember",
     "description": "Store a memory (fact/decision/preference). Refuses content that looks like a credential.",
     "inputSchema": _s("", ["content"], content=_STR, type=_MEM_TYPES, scope=_SCOPE,
                       pinned={"type": "boolean"}, importance={"type": "number"},
                       ttl_seconds={"type": "number"})},
    {"name": "memory.recall", "description": "Hybrid (lexical + semantic) search over stored memories.",
     "inputSchema": _s("", ["query"], query=_STR, scope=_SCOPE, k=_INT)},
    {"name": "memory.brief",
     "description": "Session-start context pack: project profile, pinned facts, recent episodes, open tasks.",
     "inputSchema": _s("", None, scope=_SCOPE, format={"type": "string", "enum": ["json", "markdown"]})},
    {"name": "memory.supersede",
     "description": "Replace a memory with corrected content; the old one is kept as history.",
     "inputSchema": _s("", ["id", "content"], id=_INT, content=_STR)},
    {"name": "memory.list", "description": "List active memories, newest first.",
     "inputSchema": _s("", None, scope=_SCOPE, type=_MEM_TYPES, limit=_INT)},
    {"name": "memory.forget", "description": "Delete a memory by id.",
     "inputSchema": _s("", ["id"], id=_INT)},

    {"name": "files.index", "description": "Scan and (incrementally) index a file or directory tree.",
     "inputSchema": _s("", ["path"], path=_STR)},
    {"name": "files.search", "description": "Hybrid search over indexed file contents.",
     "inputSchema": _s("", ["query"], query=_STR, k=_INT)},
    {"name": "files.similar", "description": "Find files similar to a given file.",
     "inputSchema": _s("", ["path"], path=_STR, k=_INT)},
    {"name": "files.stats", "description": "Index statistics.", "inputSchema": _s("")},

    {"name": "tasks.create",
     "description": "Create a durable multi-step task; the caller holds its write lease.",
     "inputSchema": _s("", ["goal"], goal=_STR, steps={"type": "array", "items": _STR}, scope=_SCOPE)},
    {"name": "tasks.claim",
     "description": "Take the write lease on a task (fails with lease_held if another agent holds it) and return its resume pack.",
     "inputSchema": _s("", ["task_id"], task_id=_INT)},
    {"name": "tasks.release", "description": "Give up the write lease on a task.",
     "inputSchema": _s("", ["task_id"], task_id=_INT)},
    {"name": "tasks.add_step", "description": "Append a step to a task (requires the lease).",
     "inputSchema": _s("", ["task_id", "description"], task_id=_INT, description=_STR)},
    {"name": "tasks.update_step", "description": "Update a task step's status/notes (requires the lease).",
     "inputSchema": _s("", ["step_id"], step_id=_INT,
                       status={"type": "string", "enum": ["pending", "done", "blocked", "skipped"]},
                       notes=_STR)},
    {"name": "tasks.set_status", "description": "Set task status (requires the lease).",
     "inputSchema": _s("", ["task_id", "status"], task_id=_INT,
                       status={"type": "string", "enum": ["open", "in_progress", "blocked", "done", "abandoned"]})},
    {"name": "tasks.resume", "description": "Get a resume pack to continue a task (read-only, no lease needed).",
     "inputSchema": _s("", ["task_id"], task_id=_INT)},
    {"name": "tasks.handoff",
     "description": "Release the lease, record a handoff note, and produce a handoff pack for another agent.",
     "inputSchema": _s("", ["task_id"], task_id=_INT, to=_STR, note=_STR)},
    {"name": "tasks.list", "description": "List open tasks for a scope.", "inputSchema": _s("", None, scope=_SCOPE)},

    {"name": "exec.classify",
     "description": "Dry-run the safety gate: returns SAFE / NEEDS_CONFIRMATION / HARD_BLOCK and why, without running anything.",
     "inputSchema": _s("", ["command"], command=_STR)},
    {"name": "exec.propose",
     "description": "Propose a shell command; runs if SAFE, queues for human approval if it changes state, refuses if dangerous.",
     "inputSchema": _s("", ["command"], command=_STR, cwd=_STR,
                       timeout={"type": "integer", "description": "seconds"})},
    {"name": "exec.run_approved", "description": "Run a command a human approved in the queue (single use).",
     "inputSchema": _s("", ["approval_id"], approval_id=_INT)},
    {"name": "approvals.pending", "description": "List commands awaiting human approval.", "inputSchema": _s("")},
    {"name": "approvals.decide", "description": "Approve or deny a pending command.",
     "inputSchema": _s("", ["id", "approved"], id=_INT, approved={"type": "boolean"})},

    {"name": "session.start",
     "description": "Call first in a session: returns the brief (memory + open tasks) for this workspace.",
     "inputSchema": _s("", None, scope=_SCOPE)},
    {"name": "session.end",
     "description": "Call last: stores a one-paragraph summary as an episodic memory and compacts working notes.",
     "inputSchema": _s("", ["summary"], summary=_STR, scope=_SCOPE, task_id=_INT)},
    {"name": "maintenance.run",
     "description": "Housekeeping: expire TTLs, compact working memory, decay importance, expire stale approvals/leases.",
     "inputSchema": _s("", None, scope=_SCOPE)},
]
