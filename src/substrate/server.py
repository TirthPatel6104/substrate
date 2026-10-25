"""
MCP server over stdio (JSON-RPC 2.0), implemented with the standard library only
so the daemon has zero runtime dependencies.

It implements the subset of the Model Context Protocol that agents need to
discover and call tools: ``initialize``, ``tools/list``, ``tools/call`` and
``ping``. Each tool maps to ``Substrate.dispatch``. The agent's identity (used
for memory provenance, task leases and the audit log) is taken from
``clientInfo.name`` in ``initialize`` unless ``--agent`` pins it.

For several agents sharing one daemon, see ``substrate.httpd`` (streamable
HTTP transport); this module is the one-agent-per-process transport.

Run:  python -m substrate.server --workspace myproject [--agent claude-code]
"""

from __future__ import annotations

import json
import sys

from . import __version__
from .core import TOOL_SCHEMAS, Substrate

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}


class MCPProtocol:
    """Transport-agnostic MCP request handling shared by stdio and HTTP."""

    def __init__(self, substrate: Substrate, *, actor: str | None = None) -> None:
        self.sub = substrate
        self.pinned_actor = actor
        self.actor = actor or "agent"

    def handle(self, request: dict) -> dict | None:
        """Return a JSON-RPC response dict, or None for notifications."""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            client = (params.get("clientInfo") or {}).get("name")
            if client and not self.pinned_actor:
                self.actor = str(client)[:64]
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            return _result(req_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "substrate", "version": __version__},
                "instructions": (
                    "Substrate gives you persistent memory, a file index, a durable task "
                    "ledger and gated shell execution for this workspace. Call session.start "
                    "first to load context; store durable facts with memory.remember; call "
                    "session.end with a short summary before you finish."
                ),
            })
        if method in ("notifications/initialized", "notifications/cancelled",
                      "notifications/roots/list_changed"):
            return None
        if method == "ping":
            return _result(req_id, {})
        if method == "tools/list":
            return _result(req_id, {"tools": TOOL_SCHEMAS})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            result = self.sub.dispatch(name, args, actor=self.actor)
            return _result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                "isError": "error" in result,
            })
        if method in ("resources/list", "prompts/list"):
            return _result(req_id, {method.split("/")[0]: []})
        if req_id is not None:
            return _error(req_id, -32601, f"Method not found: {method}")
        return None


def _result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class StdioMCPServer:
    def __init__(self, substrate: Substrate, *, inp=None, out=None, actor: str | None = None) -> None:
        self.sub = substrate
        self.proto = MCPProtocol(substrate, actor=actor)
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout

    def _send(self, message: dict) -> None:
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def handle(self, request: dict) -> None:
        resp = self.proto.handle(request)
        if resp is not None:
            self._send(resp)

    def serve_forever(self) -> None:  # pragma: no cover - blocking I/O loop
        for line in self.inp:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self.handle(request)
            except Exception as e:  # never crash the loop on one bad request
                if isinstance(request, dict) and request.get("id") is not None:
                    self._send(_error(request["id"], -32603, f"Internal error: {e}"))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entrypoint
    import argparse

    parser = argparse.ArgumentParser(description="Substrate MCP stdio server")
    parser.add_argument("--workspace", default="global")
    parser.add_argument("--db", default=None, help="Override database path")
    parser.add_argument("--agent", default=None,
                        help="Pin the agent identity instead of reading clientInfo.name")
    args = parser.parse_args(argv)

    sub = Substrate(workspace=args.workspace, db_path=args.db)
    try:
        StdioMCPServer(sub, actor=args.agent).serve_forever()
    finally:
        sub.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
