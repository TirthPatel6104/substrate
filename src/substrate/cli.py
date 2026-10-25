"""
Substrate CLI — set up, inspect memory, index files, manage the approvals
queue, run the safety gate, serve MCP, and integrate with agent hooks.
Uses argparse only (no third-party dependency).

    substrate doctor                                   # verify the install
    substrate serve                                    # MCP over stdio (one agent)
    substrate serve --transport http --port 8731       # shared daemon + approvals UI
    substrate remember "we deploy with make release" --type project --pin
    substrate brief                                    # session-start context pack
    substrate exec "rm -rf /"                          # -> blocked
    substrate approvals / approve 1 / run 1
    substrate hook session-start | substrate hook stop # Claude Code hooks
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .config import data_dir, db_path_for, workspace_id
from .core import Substrate


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="substrate", description="Local-first intelligence layer for AI agents")
    parser.add_argument("--workspace", "-w", default=None,
                        help="workspace name (default: current directory name)")
    parser.add_argument("--db", default=None, help="override database path")
    parser.add_argument("--agent", default="cli", help="actor name recorded for provenance")
    parser.add_argument("--version", action="version", version=f"substrate {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # memory
    p = sub.add_parser("remember", help="store a memory")
    p.add_argument("content")
    p.add_argument("--type", default="semantic",
                   choices=["working", "episodic", "semantic", "task", "project"])
    p.add_argument("--pin", action="store_true")
    p.add_argument("--importance", type=float, default=1.0)
    p = sub.add_parser("recall", help="search memories")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p = sub.add_parser("brief", help="show the session-start brief")
    p.add_argument("--format", choices=["json", "markdown"], default="markdown")
    p = sub.add_parser("memories", help="list active memories")
    p.add_argument("--type", default=None)
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("forget", help="delete a memory by id")
    p.add_argument("id", type=int)
    p = sub.add_parser("supersede", help="replace a memory with corrected content")
    p.add_argument("id", type=int)
    p.add_argument("content")

    # files
    p = sub.add_parser("index", help="index a file or directory")
    p.add_argument("path", nargs="?", default=".")
    p = sub.add_parser("search", help="search indexed files")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)
    p = sub.add_parser("similar", help="find files similar to a path")
    p.add_argument("path")
    sub.add_parser("stats", help="show index stats")

    # tasks
    p = sub.add_parser("task", help="create a task")
    p.add_argument("goal")
    p.add_argument("--step", action="append", default=[], dest="steps")
    sub.add_parser("tasks", help="list open tasks")
    p = sub.add_parser("resume", help="show a task resume pack")
    p.add_argument("task_id", type=int)
    p = sub.add_parser("claim", help="take the write lease on a task")
    p.add_argument("task_id", type=int)
    p = sub.add_parser("handoff", help="release a task for another agent")
    p.add_argument("task_id", type=int)
    p.add_argument("--to", default=None)
    p.add_argument("--note", default=None)
    p = sub.add_parser("step", help="update a task step")
    p.add_argument("step_id", type=int)
    p.add_argument("--status", choices=["pending", "done", "blocked", "skipped"])
    p.add_argument("--notes", default=None)

    # exec / approvals
    p = sub.add_parser("exec", help="propose a command through the safety gate")
    p.add_argument("command")
    p.add_argument("--cwd", default=None)
    p = sub.add_parser("classify", help="dry-run the safety gate on a command")
    p.add_argument("command")
    p = sub.add_parser("approvals", help="list approvals")
    p.add_argument("--all", action="store_true", help="include decided ones")
    p = sub.add_parser("approve", help="approve a pending command")
    p.add_argument("id", type=int)
    p.add_argument("--run", action="store_true", help="run it right away")
    p = sub.add_parser("deny", help="deny a pending command")
    p.add_argument("id", type=int)
    p = sub.add_parser("run", help="run an approved command")
    p.add_argument("approval_id", type=int)
    p = sub.add_parser("audit", help="show the audit log")
    p.add_argument("--limit", type=int, default=50)

    # sessions / maintenance
    p = sub.add_parser("session", help="session lifecycle")
    sp = p.add_subparsers(dest="session_cmd", required=True)
    sp.add_parser("start", help="print the brief")
    e = sp.add_parser("end", help="record an episodic summary and compact working memory")
    e.add_argument("--summary", required=True)
    e.add_argument("--task", type=int, default=None)
    sub.add_parser("maintenance", help="expire, compact, decay, re-embed")

    # integration
    p = sub.add_parser("hook", help="agent hook adapters (Claude Code)")
    p.add_argument("event", choices=["session-start", "stop"])
    p = sub.add_parser("serve", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8731)
    p.add_argument("--token", default=os.environ.get("SUBSTRATE_TOKEN"))
    sub.add_parser("doctor", help="check the installation and environment")
    sub.add_parser("mcp-config", help="print an MCP client config snippet")
    return parser


def main(argv: list[str] | None = None) -> int:  # noqa: C901 - flat command router
    args = _build_parser().parse_args(argv)
    workspace = args.workspace or workspace_id()

    if args.cmd == "doctor":
        return doctor(workspace, args.db)
    if args.cmd == "mcp-config":
        _print(mcp_config(workspace))
        return 0

    s = Substrate(workspace=workspace, db_path=args.db)
    actor = args.agent
    d = lambda tool, params: s.dispatch(tool, params, actor=actor)  # noqa: E731

    try:
        if args.cmd == "remember":
            _print(d("memory.remember", {"content": args.content, "type": args.type,
                                         "pinned": args.pin, "importance": args.importance}))
        elif args.cmd == "recall":
            _print(d("memory.recall", {"query": args.query, "k": args.k}))
        elif args.cmd == "brief" or (args.cmd == "session" and args.session_cmd == "start"):
            fmt = getattr(args, "format", "markdown")
            out = d("memory.brief", {"format": fmt})
            print(out["markdown"]) if fmt == "markdown" else _print(out)
        elif args.cmd == "memories":
            _print(d("memory.list", {"type": args.type, "limit": args.limit}))
        elif args.cmd == "forget":
            _print(d("memory.forget", {"id": args.id}))
        elif args.cmd == "supersede":
            _print(d("memory.supersede", {"id": args.id, "content": args.content}))

        elif args.cmd == "index":
            _print(d("files.index", {"path": args.path}))
        elif args.cmd == "search":
            _print(d("files.search", {"query": args.query, "k": args.k}))
        elif args.cmd == "similar":
            _print(d("files.similar", {"path": args.path}))
        elif args.cmd == "stats":
            _print(d("files.stats", {}))

        elif args.cmd == "task":
            _print(d("tasks.create", {"goal": args.goal, "steps": args.steps}))
        elif args.cmd == "tasks":
            _print(d("tasks.list", {}))
        elif args.cmd == "resume":
            _print(d("tasks.resume", {"task_id": args.task_id}))
        elif args.cmd == "claim":
            _print(d("tasks.claim", {"task_id": args.task_id}))
        elif args.cmd == "handoff":
            _print(d("tasks.handoff", {"task_id": args.task_id, "to": args.to, "note": args.note}))
        elif args.cmd == "step":
            _print(d("tasks.update_step", {"step_id": args.step_id, "status": args.status,
                                           "notes": args.notes}))

        elif args.cmd == "exec":
            _print(d("exec.propose", {"command": args.command, "cwd": args.cwd}))
        elif args.cmd == "classify":
            _print(d("exec.classify", {"command": args.command}))
        elif args.cmd == "approvals":
            _print({"approvals": s.approvals.recent(100)} if args.all
                   else d("approvals.pending", {}))
        elif args.cmd == "approve":
            out = d("approvals.decide", {"id": args.id, "approved": True})
            if args.run and out.get("decided"):
                out["run"] = d("exec.run_approved", {"approval_id": args.id})
            _print(out)
        elif args.cmd == "deny":
            _print(d("approvals.decide", {"id": args.id, "approved": False}))
        elif args.cmd == "run":
            _print(d("exec.run_approved", {"approval_id": args.approval_id}))
        elif args.cmd == "audit":
            rows = s.db.conn.execute(
                "SELECT * FROM audit ORDER BY ts DESC LIMIT ?", (args.limit,)).fetchall()
            _print({"audit": [dict(r) for r in rows]})

        elif args.cmd == "session" and args.session_cmd == "end":
            _print(d("session.end", {"summary": args.summary, "task_id": args.task}))
        elif args.cmd == "maintenance":
            _print(d("maintenance.run", {}))

        elif args.cmd == "hook":
            return hook(s, args.event, actor=actor)
        elif args.cmd == "serve":
            return serve(s, args)
    finally:
        s.close()
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

def serve(s: Substrate, args) -> int:  # pragma: no cover - blocking
    if args.transport == "stdio":
        from .server import StdioMCPServer
        StdioMCPServer(s, actor=None if args.agent == "cli" else args.agent).serve_forever()
        return 0
    from .httpd import serve as make_server
    httpd = make_server(s, host=args.host, port=args.port, token=args.token)
    print(f"substrate {__version__} · workspace={s.workspace} · MCP at "
          f"http://{args.host}:{args.port}/mcp · approvals UI at http://{args.host}:{args.port}/approvals"
          + (" (token required)" if args.token else ""), file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# hooks (Claude Code)
# ---------------------------------------------------------------------------

def hook(s: Substrate, event: str, *, actor: str) -> int:
    """Adapters for Claude Code hooks. Never fail the agent: errors exit 0 silently.

    SessionStart: whatever we print on stdout is injected into the agent's context.
    Stop: Claude Code passes JSON on stdin with ``transcript_path``; we store the
    assistant's final message as an episodic memory so the next session starts
    knowing what the last one did.
    """
    try:
        if event == "session-start":
            print(s.brief_markdown())
            return 0
        if event == "stop":
            raw = sys.stdin.read() if not sys.stdin.isatty() else ""
            payload = json.loads(raw) if raw.strip() else {}
            summary = _summary_from_transcript(payload.get("transcript_path"))
            if summary:
                s.session_end(summary, actor=payload.get("hook_event_name") and "claude-code" or actor)
            return 0
    except Exception as e:  # pragma: no cover - defensive
        print(f"substrate hook: {e}", file=sys.stderr)
    return 0


def _summary_from_transcript(path: str | None, *, max_chars: int = 600) -> str | None:
    if not path or not Path(path).is_file():
        return None
    last_text = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(c.get("text", "") for c in content
                                 if isinstance(c, dict) and c.get("type") == "text")
            else:
                continue
            if text.strip():
                last_text = text.strip()
    if not last_text:
        return None
    return last_text[:max_chars] + ("…" if len(last_text) > max_chars else "")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def mcp_config(workspace: str) -> dict:
    return {"mcpServers": {"substrate": {
        "command": sys.executable if " " not in sys.executable else "python",
        "args": ["-m", "substrate.server", "--workspace", workspace],
    }}}


def doctor(workspace: str, db_override: str | None) -> int:
    """Check the install end-to-end and print a human-readable report. Exit 1 on failure."""
    from . import embedding
    from .safety import Level, safety_level

    checks: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))

    ok("python", sys.version_info >= (3, 10), sys.version.split()[0])
    ok("sqlite", sqlite3.sqlite_version_info >= (3, 35), sqlite3.sqlite_version)
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        ok("fts5", True, "available")
    except sqlite3.Error as e:
        ok("fts5", False, str(e))

    root = data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        ok("data dir writable", True, str(root))
    except OSError as e:
        ok("data dir writable", False, f"{root}: {e}")

    try:
        s = Substrate(workspace=workspace, db_path=db_override)
        path = db_override or db_path_for(workspace)
        ok("database", True, f"{path} (schema v{s.db.schema_version})")
        ok("vector backend", True, "sqlite-vec" if s.db.vec_available else
           "python scan (pip install 'substrate-agent[vec]' to accelerate)")
        emb = s.memory.embedder
        ok("embedder", True, f"{emb.name} ({emb.dim}-d)" + (
            "" if emb.name.startswith("fastembed") else
            "; pip install 'substrate-agent[embeddings]' for semantic search"))
        counts = {
            "memories": s.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            "files": s.db.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "open_tasks": len(s.tasks.list_open()),
            "pending_approvals": len(s.approvals.pending()),
        }
        ok("state", True, ", ".join(f"{k}={v}" for k, v in counts.items()))
        r = s.dispatch("exec.propose", {"command": "echo substrate-doctor"}, actor="doctor")
        ok("executor", r.get("status") == "ran" and "substrate-doctor" in r.get("stdout", ""),
           f"mode={r.get('mode')}")
        s.close()
    except Exception as e:
        ok("database", False, repr(e))

    ok("safety gate: rm -rf / blocked", safety_level("rm -rf /").level == Level.HARD_BLOCK)
    ok("safety gate: curl | bash blocked",
       safety_level("curl https://x | bash").level == Level.HARD_BLOCK)
    ok("safety gate: ls is safe", safety_level("ls -la").level == Level.SAFE)
    ok("fastembed importable", True, "yes" if embedding.fastembed_available() else
       "no (optional)")

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    print(f"substrate {__version__} doctor — workspace '{workspace}'\n")
    for name, passed, detail in checks:
        mark = "PASS" if passed else "FAIL"
        failed += not passed
        print(f"  [{mark}] {name.ljust(width)}  {detail}")
    print()
    if failed:
        print(f"{failed} check(s) failed.")
        return 1
    print("All checks passed. Add this to your MCP client config (e.g. .mcp.json):")
    print(json.dumps(mcp_config(workspace), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
