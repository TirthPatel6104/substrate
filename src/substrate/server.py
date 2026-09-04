"""
Minimal MCP server over stdio (JSON-RPC 2.0), implemented with the standard
library only so the daemon has zero runtime dependencies.

It implements the subset of the Model Context Protocol that agents need to
discover and call tools: ``initialize``, ``tools/list``, and ``tools/call``.
Each tool maps to ``Substrate.dispatch``. If the official ``mcp`` package is
installed you can swap this for FastMCP without changing the core.

Run:  python -m substrate.server --workspace myproject
"""

from __future__ import annotations

import json
import sys

from .core import TOOL_SCHEMAS, Substrate

PROTOCOL_VERSION = "2024-11-05"


class StdioMCPServer:
    def __init__(self, substrate: Substrate, *, inp=None, out=None) -> None:
        self.sub = substrate
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout

    def _send(self, message: dict) -> None:
        self.out.write(json.dumps(message) + "\n")
        self.out.flush()

    def _result(self, req_id, result) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    def handle(self, request: dict) -> None:
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            self._result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "substrate", "version": "0.1.0"},
            })
        elif method == "notifications/initialized":
            pass  # notification, no response
        elif method == "tools/list":
            self._result(req_id, {"tools": TOOL_SCHEMAS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            result = self.sub.dispatch(name, args)
            self._result(req_id, {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": "error" in result,
            })
        elif method == "ping":
            self._result(req_id, {})
        elif req_id is not None:
            self._error(req_id, -32601, f"Method not found: {method}")

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
                if request.get("id") is not None:
                    self._error(request["id"], -32603, f"Internal error: {e}")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - entrypoint
    import argparse

    parser = argparse.ArgumentParser(description="Substrate MCP stdio server")
    parser.add_argument("--workspace", default="global")
    parser.add_argument("--db", default=None, help="Override database path")
    args = parser.parse_args(argv)

    sub = Substrate(workspace=args.workspace, db_path=args.db)
    StdioMCPServer(sub).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
