"""
Substrate core: wires the database and all subsystems together behind one
object, and exposes a single ``dispatch(tool, params)`` entry point used by both
the MCP server and the CLI so they can never drift apart.
"""

from __future__ import annotations

from pathlib import Path

from .approvals import ApprovalQueue
from .config import db_path_for
from .db import Database
from .executor import SafeExecutor
from .files import FileIndex
from .memory import MemoryStore, SecretRefused
from .tasks import TaskLedger


class Substrate:
    def __init__(self, workspace: str = "global", db_path: str | Path | None = None) -> None:
        self.workspace = workspace
        self.db = Database(db_path or db_path_for(workspace))
        self.memory = MemoryStore(self.db)
        self.files = FileIndex(self.db)
        self.tasks = TaskLedger(self.db)
        self.approvals = ApprovalQueue(self.db)
        self.executor = SafeExecutor(self.db)

    def close(self) -> None:
        self.db.close()

    # -- unified tool dispatch ------------------------------------------------
    def dispatch(self, tool: str, params: dict, *, actor: str = "agent") -> dict:
        """Route an MCP-style tool call to the right subsystem."""
        p = params or {}
        scope = p.get("scope") or self.workspace

        try:
            if tool == "memory.remember":
                mid = self.memory.remember(
                    p["content"],
                    mem_type=p.get("type", "semantic"),
                    scope=scope,
                    source=p.get("source", actor),
                    pinned=bool(p.get("pinned", False)),
                    ttl_seconds=p.get("ttl_seconds"),
                )
                return {"id": mid}
            if tool == "memory.recall":
                return {"results": self.memory.recall(p["query"], scope=scope, k=p.get("k", 5))}
            if tool == "memory.brief":
                if p.get("format") == "markdown":
                    return {"markdown": self.memory.render_brief_markdown(scope)}
                return self.memory.brief(scope)
            if tool == "memory.forget":
                return {"forgotten": self.memory.forget(int(p["id"]))}

            if tool == "files.index":
                return self.files.index_path(p["path"])
            if tool == "files.search":
                return {"results": self.files.search(p["query"], k=p.get("k", 8))}
            if tool == "files.similar":
                return {"results": self.files.similar_files(p["path"], k=p.get("k", 5))}
            if tool == "files.stats":
                return self.files.stats()

            if tool == "tasks.create":
                tid = self.tasks.create(p["goal"], p.get("steps"), scope=scope)
                return {"task_id": tid}
            if tool == "tasks.update_step":
                ok = self.tasks.update_step(
                    int(p["step_id"]), status=p.get("status"), notes=p.get("notes")
                )
                return {"updated": ok}
            if tool == "tasks.resume":
                pack = self.tasks.resume(int(p["task_id"]))
                return pack or {"error": "no such task"}
            if tool == "tasks.handoff":
                pack = self.tasks.handoff(int(p["task_id"]), p.get("to"))
                return pack or {"error": "no such task"}
            if tool == "tasks.list":
                return {"tasks": self.tasks.list_open(scope=scope)}

            if tool == "exec.propose":
                return self.executor.propose(p["command"], actor=actor).to_dict()
            if tool == "exec.run_approved":
                return self.executor.run_approved(int(p["approval_id"]), actor=actor).to_dict()
            if tool == "approvals.pending":
                return {"pending": self.approvals.pending()}
            if tool == "approvals.decide":
                ok = self.approvals.decide(
                    int(p["id"]), bool(p["approved"]), decided_by=actor
                )
                return {"decided": ok}

            return {"error": f"unknown tool '{tool}'"}
        except SecretRefused as e:
            return {"error": str(e), "code": "secret_refused"}
        except KeyError as e:
            return {"error": f"missing required parameter: {e}"}
        except (ValueError, TypeError) as e:
            return {"error": str(e)}


# Public catalog of tools, used to advertise capabilities over MCP.
TOOL_SCHEMAS: list[dict] = [
    {"name": "memory.remember", "description": "Store a memory (fact/decision/preference).",
     "inputSchema": {"type": "object", "required": ["content"], "properties": {
         "content": {"type": "string"},
         "type": {"type": "string", "enum": ["working", "episodic", "semantic", "task", "project"]},
         "scope": {"type": "string"}, "pinned": {"type": "boolean"},
         "ttl_seconds": {"type": "number"}}}},
    {"name": "memory.recall", "description": "Hybrid search over stored memories.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "scope": {"type": "string"}, "k": {"type": "integer"}}}},
    {"name": "memory.brief", "description": "Session-start context pack for a workspace.",
     "inputSchema": {"type": "object", "properties": {
         "scope": {"type": "string"}, "format": {"type": "string", "enum": ["json", "markdown"]}}}},
    {"name": "memory.forget", "description": "Delete a memory by id.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}}},
    {"name": "files.index", "description": "Scan and index a file or directory tree.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "files.search", "description": "Hybrid search over indexed file contents.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"}, "k": {"type": "integer"}}}},
    {"name": "files.similar", "description": "Find files similar to a given file.",
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string"}, "k": {"type": "integer"}}}},
    {"name": "files.stats", "description": "Index statistics.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "tasks.create", "description": "Create a durable multi-step task.",
     "inputSchema": {"type": "object", "required": ["goal"], "properties": {
         "goal": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}},
         "scope": {"type": "string"}}}},
    {"name": "tasks.update_step", "description": "Update a task step's status/notes.",
     "inputSchema": {"type": "object", "required": ["step_id"], "properties": {
         "step_id": {"type": "integer"},
         "status": {"type": "string", "enum": ["pending", "done", "blocked", "skipped"]},
         "notes": {"type": "string"}}}},
    {"name": "tasks.resume", "description": "Get a resume pack to continue a task.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "integer"}}}},
    {"name": "tasks.handoff", "description": "Freeze a task and produce a handoff doc.",
     "inputSchema": {"type": "object", "required": ["task_id"], "properties": {
         "task_id": {"type": "integer"}, "to": {"type": "string"}}}},
    {"name": "tasks.list", "description": "List open tasks for a scope.",
     "inputSchema": {"type": "object", "properties": {"scope": {"type": "string"}}}},
    {"name": "exec.propose", "description": "Propose a shell command; runs if SAFE, queues if it needs confirmation, refuses if dangerous.",
     "inputSchema": {"type": "object", "required": ["command"], "properties": {
         "command": {"type": "string"}}}},
    {"name": "exec.run_approved", "description": "Run a command a human approved in the queue.",
     "inputSchema": {"type": "object", "required": ["approval_id"], "properties": {
         "approval_id": {"type": "integer"}}}},
    {"name": "approvals.pending", "description": "List commands awaiting human approval.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "approvals.decide", "description": "Approve or deny a pending command.",
     "inputSchema": {"type": "object", "required": ["id", "approved"], "properties": {
         "id": {"type": "integer"}, "approved": {"type": "boolean"}}}},
]
