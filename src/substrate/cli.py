"""
Substrate CLI — setup, inspect memory, index files, manage the approvals queue,
and run the safety gate. Uses argparse only (no third-party dependency).

Examples:
    substrate remember "we deploy with make release" --type project --pin
    substrate recall "how do we deploy"
    substrate brief --format markdown
    substrate index ./src
    substrate exec "rm -rf /"            # -> blocked
    substrate exec "pip install requests" # -> queued for approval
    substrate approvals
    substrate approve 1
    substrate run 1
"""

from __future__ import annotations

import argparse
import json
import sys

from .core import Substrate


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="substrate", description="Local-first intelligence layer for AI agents")
    parser.add_argument("--workspace", "-w", default="global")
    parser.add_argument("--db", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("remember", help="Store a memory")
    p.add_argument("content")
    p.add_argument("--type", default="semantic",
                   choices=["working", "episodic", "semantic", "task", "project"])
    p.add_argument("--pin", action="store_true")

    p = sub.add_parser("recall", help="Search memories")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)

    p = sub.add_parser("brief", help="Show the session-start brief")
    p.add_argument("--format", choices=["json", "markdown"], default="markdown")

    p = sub.add_parser("forget", help="Delete a memory by id")
    p.add_argument("id", type=int)

    p = sub.add_parser("index", help="Index a file or directory")
    p.add_argument("path")

    p = sub.add_parser("search", help="Search indexed files")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=8)

    p = sub.add_parser("similar", help="Find files similar to a path")
    p.add_argument("path")

    p = sub.add_parser("task", help="Create a task")
    p.add_argument("goal")
    p.add_argument("--step", action="append", default=[], dest="steps")

    p = sub.add_parser("tasks", help="List open tasks")

    p = sub.add_parser("resume", help="Show a task resume pack")
    p.add_argument("task_id", type=int)

    p = sub.add_parser("exec", help="Propose a command through the safety gate")
    p.add_argument("command")

    sub.add_parser("approvals", help="List pending approvals")

    p = sub.add_parser("approve", help="Approve a pending command")
    p.add_argument("id", type=int)

    p = sub.add_parser("deny", help="Deny a pending command")
    p.add_argument("id", type=int)

    p = sub.add_parser("run", help="Run an approved command")
    p.add_argument("approval_id", type=int)

    sub.add_parser("stats", help="Show index stats")

    args = parser.parse_args(argv)
    s = Substrate(workspace=args.workspace, db_path=args.db)

    try:
        if args.cmd == "remember":
            _print(s.dispatch("memory.remember",
                              {"content": args.content, "type": args.type, "pinned": args.pin}))
        elif args.cmd == "recall":
            _print(s.dispatch("memory.recall", {"query": args.query, "k": args.k}))
        elif args.cmd == "brief":
            out = s.dispatch("memory.brief", {"format": args.format})
            if args.format == "markdown":
                print(out["markdown"])
            else:
                _print(out)
        elif args.cmd == "forget":
            _print(s.dispatch("memory.forget", {"id": args.id}))
        elif args.cmd == "index":
            _print(s.dispatch("files.index", {"path": args.path}))
        elif args.cmd == "search":
            _print(s.dispatch("files.search", {"query": args.query, "k": args.k}))
        elif args.cmd == "similar":
            _print(s.dispatch("files.similar", {"path": args.path}))
        elif args.cmd == "task":
            _print(s.dispatch("tasks.create", {"goal": args.goal, "steps": args.steps}))
        elif args.cmd == "tasks":
            _print(s.dispatch("tasks.list", {}))
        elif args.cmd == "resume":
            _print(s.dispatch("tasks.resume", {"task_id": args.task_id}))
        elif args.cmd == "exec":
            _print(s.dispatch("exec.propose", {"command": args.command}))
        elif args.cmd == "approvals":
            _print(s.dispatch("approvals.pending", {}))
        elif args.cmd == "approve":
            _print(s.dispatch("approvals.decide", {"id": args.id, "approved": True}))
        elif args.cmd == "deny":
            _print(s.dispatch("approvals.decide", {"id": args.id, "approved": False}))
        elif args.cmd == "run":
            _print(s.dispatch("exec.run_approved", {"approval_id": args.approval_id}))
        elif args.cmd == "stats":
            _print(s.dispatch("files.stats", {}))
    finally:
        s.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
