"""HTTP transport: MCP sessions + identity, approvals UI, auth and CSRF guards."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from substrate.core import Substrate
from substrate.httpd import serve


@pytest.fixture
def http(vector_backend):
    sub = Substrate(workspace="http-test", db_path=":memory:")
    server = serve(sub, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, sub
    server.shutdown()
    server.server_close()
    sub.close()


def _req(base, path, method="GET", body=None, headers=None, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            payload = r.read()
            return r.status, dict(r.headers), (payload if raw else (json.loads(payload) if payload else None))
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            return e.code, dict(e.headers), json.loads(payload)
        except Exception:
            return e.code, dict(e.headers), payload


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_healthz(http):
    base, _ = http
    status, _, body = _req(base, "/healthz")
    assert status == 200 and body["status"] == "ok" and body["tools"] > 20


def test_initialize_creates_session_and_identity(http):
    base, sub = http
    status, headers, body = _req(base, "/mcp", "POST", _rpc(
        "initialize", {"protocolVersion": "2025-03-26", "clientInfo": {"name": "codex"}}))
    assert status == 200
    sid = headers["Mcp-Session-Id"]
    assert body["result"]["protocolVersion"] == "2025-03-26"

    # notifications get 202 with no body
    status, _, _ = _req(base, "/mcp", "POST", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                        headers={"Mcp-Session-Id": sid})
    assert status == 202

    status, _, body = _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "memory.remember", "arguments": {"content": "from codex"}}, 2),
        headers={"Mcp-Session-Id": sid})
    assert status == 200 and body["result"]["isError"] is False
    row = sub.db.conn.execute("SELECT source FROM memories").fetchone()
    assert row["source"] == "codex"

    # session-less call falls back to the header identity
    _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "memory.remember", "arguments": {"content": "from header"}}, 3),
        headers={"X-Substrate-Agent": "opencode"})
    sources = {r["source"] for r in sub.db.conn.execute("SELECT source FROM memories")}
    assert sources == {"codex", "opencode"}

    status, _, _ = _req(base, "/mcp", "DELETE", headers={"Mcp-Session-Id": sid})
    assert status == 204


def test_two_agents_share_one_daemon_with_leases(http):
    base, sub = http

    def session(name):
        _, h, _ = _req(base, "/mcp", "POST", _rpc(
            "initialize", {"clientInfo": {"name": name}}))
        return {"Mcp-Session-Id": h["Mcp-Session-Id"]}

    a, b = session("claude-code"), session("codex")
    _, _, created = _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "tasks.create", "arguments": {"goal": "migrate", "steps": ["s1", "s2"]}}),
        headers=a)
    task_id = json.loads(created["result"]["content"][0]["text"])["task_id"]
    step_id = sub.db.conn.execute("SELECT id FROM task_steps WHERE task_id=?", (task_id,)).fetchone()[0]

    _, _, denied = _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "tasks.update_step", "arguments": {"step_id": step_id, "status": "done"}}),
        headers=b)
    payload = json.loads(denied["result"]["content"][0]["text"])
    assert denied["result"]["isError"] is True
    assert payload["code"] == "lease_held" and payload["lease_owner"] == "claude-code"

    _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "tasks.handoff", "arguments": {"task_id": task_id, "to": "codex",
                                                              "note": "s1 needs the auth fix"}}),
        headers=a)
    _, _, ok = _req(base, "/mcp", "POST", _rpc(
        "tools/call", {"name": "tasks.update_step", "arguments": {"step_id": step_id, "status": "done"}}),
        headers=b)
    assert ok["result"]["isError"] is False
    pack = sub.tasks.resume(task_id)
    assert pack["progress"] == "1/2 steps done" and pack["lease_owner"] == "codex"


def test_batch_requests(http):
    base, _ = http
    status, _, body = _req(base, "/mcp", "POST", [_rpc("ping", id_=1), _rpc("tools/list", id_=2)])
    assert status == 200 and [r["id"] for r in body] == [1, 2]


def test_mcp_requires_json_content_type(http):
    base, _ = http
    status, _, _ = _req(base, "/mcp", "POST", "a=b", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415


def test_approvals_page_and_actions(http):
    base, sub = http
    res = sub.dispatch("exec.propose", {"command": "mv a b"}, actor="claude-code")
    aid = res["approval_id"]
    status, _, page = _req(base, "/approvals", raw=True)
    assert status == 200 and b"mv a b" in page and b"Approve" in page

    status, _, body = _req(base, f"/approvals/{aid}/deny", "POST", "",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 200 and body["decided"] is True
    assert sub.approvals.get(aid)["status"] == "denied"

    res2 = sub.dispatch("exec.propose", {"command": "echo queued > out.txt"}, actor="claude-code")
    status, _, body = _req(base, f"/approvals/{res2['approval_id']}/approve", "POST", "run=0",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert body["decided"] is True and sub.approvals.get(res2["approval_id"])["status"] == "approved"


def test_cross_origin_approval_is_rejected(http):
    base, sub = http
    aid = sub.dispatch("exec.propose", {"command": "mv a b"})["approval_id"]
    status, _, _ = _req(base, f"/approvals/{aid}/approve", "POST", "",
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Origin": "https://evil.example"})
    assert status == 403
    assert sub.approvals.get(aid)["status"] == "pending"
    status, _, _ = _req(base, f"/approvals/{aid}/approve", "POST", "",
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_bearer_token_required_when_configured(vector_backend):
    sub = Substrate(workspace="tok", db_path=":memory:")
    server = serve(sub, host="127.0.0.1", port=0, token="s3cret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _req(base, "/healthz")[0] == 200            # liveness is open
        assert _req(base, "/api/approvals")[0] == 401
        assert _req(base, "/mcp", "POST", _rpc("ping"))[0] == 401
        assert _req(base, "/mcp", "POST", _rpc("ping"), headers={"Authorization": "Bearer s3cret"})[0] == 200
        assert _req(base, "/approvals?token=s3cret", raw=True)[0] == 200
    finally:
        server.shutdown()
        server.server_close()
        sub.close()
