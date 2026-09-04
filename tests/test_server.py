"""MCP stdio server protocol tests (in-process, no subprocess)."""

import io
import json

from substrate.core import Substrate
from substrate.server import StdioMCPServer


def _roundtrip(server, request):
    out = io.StringIO()
    server.out = out
    server.handle(request)
    lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def test_initialize_and_list_tools():
    sub = Substrate(workspace="test", db_path=":memory:")
    server = StdioMCPServer(sub)

    init = _roundtrip(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "substrate"

    listed = _roundtrip(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in listed["result"]["tools"]}
    assert "memory.remember" in names
    assert "exec.propose" in names
    sub.close()


def test_tools_call_dispatches():
    sub = Substrate(workspace="test", db_path=":memory:")
    server = StdioMCPServer(sub)

    resp = _roundtrip(server, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "memory.remember", "arguments": {"content": "hello from mcp"}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "id" in payload
    assert resp["result"]["isError"] is False

    resp = _roundtrip(server, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "exec.propose", "arguments": {"command": "rm -rf /"}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "blocked"
    sub.close()


def test_unknown_method_errors():
    sub = Substrate(workspace="test", db_path=":memory:")
    server = StdioMCPServer(sub)
    resp = _roundtrip(server, {"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
    assert resp["error"]["code"] == -32601
    sub.close()
