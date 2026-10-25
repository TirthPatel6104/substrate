"""
Two agents, one daemon: a task started by one agent is finished by another.

Starts the HTTP daemon in-process, connects two MCP clients ("claude-code" and
"codex") over streamable HTTP, and walks through:

  1. claude-code creates a 3-step task and finishes step 1 (it holds the lease)
  2. codex tries to write to the task -> structured `lease_held` error
  3. claude-code hands off with a note; codex claims, reads the resume pack,
     finishes the remaining steps
  4. codex ends its session; the episodic memory shows up in the next brief

Run:  python examples/multi_agent_handoff.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SUBSTRATE_EMBEDDER", "hashing")

from substrate.core import Substrate  # noqa: E402
from substrate.httpd import serve  # noqa: E402


class Agent:
    """A tiny MCP-over-HTTP client, enough for the demo."""

    def __init__(self, base: str, name: str) -> None:
        self.base, self.name, self._id = base, name, 0
        self.session = None
        self.session = self._rpc("initialize", {"clientInfo": {"name": name}})["session"]

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method,
                           "params": params}).encode()
        headers = {"Content-Type": "application/json"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.base + "/mcp", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req) as r:
            out = json.loads(r.read())
            out["session"] = r.headers.get("Mcp-Session-Id") or self.session
            return out

    def call(self, tool: str, **args) -> dict:
        resp = self._rpc("tools/call", {"name": tool, "arguments": args})
        result = json.loads(resp["result"]["content"][0]["text"])
        tag = "ERR " if resp["result"]["isError"] else "ok  "
        short = json.dumps(result, default=str)
        print(f"[{self.name:11}] {tag}{tool:18} {short[:110]}{'…' if len(short) > 110 else ''}")
        return result


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        sub = Substrate(workspace="demo", db_path=Path(td) / "demo.db")
        server = serve(sub, host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"daemon up at {base}  (approvals UI: {base}/approvals)\n")

        claude = Agent(base, "claude-code")
        codex = Agent(base, "codex")

        print("-- 1. claude-code starts a task and does step 1")
        claude.call("memory.remember", content="Auth middleware lives in app/auth.py; tests under tests/auth/",
                    type="project", pinned=True)
        task = claude.call("tasks.create", goal="Migrate 3 endpoints to the new auth middleware",
                           steps=["/login", "/logout", "/refresh"])
        tid = task["task_id"]
        pack = claude.call("tasks.resume", task_id=tid)
        claude.call("tasks.update_step", step_id=pack["next_step"]["step_id"], status="done",
                    notes="/login done; needed the token-refresh edge case handled in auth.py:88")

        print("\n-- 2. codex tries to write to the same task")
        pack = codex.call("tasks.resume", task_id=tid)
        codex.call("tasks.update_step", step_id=pack["next_step"]["step_id"], status="done")

        print("\n-- 3. claude-code hands off; codex claims and finishes")
        claude.call("tasks.handoff", task_id=tid, to="codex",
                    note="context budget exhausted; /logout and /refresh remain")
        pack = codex.call("tasks.claim", task_id=tid)
        for step in pack["remaining_steps"]:
            codex.call("tasks.update_step", step_id=step["step_id"], status="done")
        codex.call("tasks.resume", task_id=tid)

        print("\n-- 4. codex ends its session; the next session's brief knows what happened")
        codex.call("session.end", summary="Finished the auth migration: /logout and /refresh moved; all tests green")
        brief = claude.call("memory.brief", format="markdown")
        print("\n" + brief["markdown"])

        server.shutdown()
        server.server_close()
        sub.close()


if __name__ == "__main__":
    main()
