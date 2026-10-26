"""End-to-end: spawn the real stdio server as an MCP client would and talk JSON-RPC."""

import json
import os
import subprocess
import sys


def test_stdio_server_roundtrip(tmp_path):
    env = dict(os.environ, SUBSTRATE_EMBEDDER="hashing", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "substrate.server", "--workspace", "e2e",
         "--db", str(tmp_path / "e2e.db")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env=env,
    )
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "pytest-client"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory.remember", "arguments": {"content": "e2e fact", "type": "project"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "exec.propose", "arguments": {"command": "rm -rf /"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "memory.brief", "arguments": {"format": "markdown"}}},
    ]
    try:
        out, err = proc.communicate("\n".join(json.dumps(m) for m in messages) + "\n", timeout=60)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    responses = {r["id"]: r for r in (json.loads(ln) for ln in out.splitlines() if ln.strip())}
    assert set(responses) == {1, 2, 3, 4, 5}, "one response per request, none for the notification"
    assert responses[1]["result"]["protocolVersion"] == "2025-06-18"
    assert len(responses[2]["result"]["tools"]) >= 27
    blocked = json.loads(responses[4]["result"]["content"][0]["text"])
    assert blocked["status"] == "blocked" and responses[4]["result"]["isError"] is False
    brief = json.loads(responses[5]["result"]["content"][0]["text"])["markdown"]
    assert "e2e fact" in brief
