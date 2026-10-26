"""
Multi-agent daemon: MCP over streamable HTTP + a human approvals page, using
only the standard library.

    substrate serve --transport http --port 8731

Endpoints
---------
POST   /mcp                      JSON-RPC 2.0 (MCP streamable-HTTP, JSON responses)
DELETE /mcp                      end the session named by Mcp-Session-Id
GET    /healthz                  liveness + backend info (unauthenticated)
GET    /approvals                human approvals page (HTML)
POST   /approvals/<id>/approve   approve (optionally run) a pending command
POST   /approvals/<id>/deny      deny a pending command
GET    /api/approvals            pending approvals as JSON
GET    /api/audit                last 200 audit rows as JSON

Identity: every ``initialize`` creates a session; ``clientInfo.name`` becomes
the actor for that session (provenance, leases, audit). Agents that cannot
keep a session may send ``X-Substrate-Agent`` instead.

Security model (local daemon, not an internet service):
* binds 127.0.0.1 by default; ``--host 0.0.0.0`` is a deliberate opt-in;
* optional bearer token (``SUBSTRATE_TOKEN``) required on everything but /healthz;
* state-changing browser routes reject cross-origin requests (CSRF) by checking
  ``Origin``/``Sec-Fetch-Site``: a web page you happen to visit cannot approve a
  queued ``rm -rf`` on your behalf;
* ``/mcp`` requires ``Content-Type: application/json``, which HTML forms cannot
  send without a CORS preflight that we never grant.
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .core import TOOL_SCHEMAS, Substrate
from .server import MCPProtocol

DEFAULT_PORT = 8731


class SubstrateHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # On POSIX, SO_REUSEADDR only lets us rebind a port left in TIME_WAIT after a
    # restart. On Windows it additionally lets a *second* listener bind an
    # in-use port, which both races our own tests and would let another local
    # process hijack the daemon's port, so it stays off there.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, addr: tuple[str, int], substrate: Substrate, *, token: str | None = None) -> None:
        super().__init__(addr, Handler)
        self.sub = substrate
        self.token = token
        self.sessions: dict[str, MCPProtocol] = {}
        self.sessions_lock = threading.Lock()
        self.started = time.time()

    def session(self, sid: str | None, fallback_actor: str | None) -> MCPProtocol:
        with self.sessions_lock:
            if sid and sid in self.sessions:
                return self.sessions[sid]
        # Session-less call: a throwaway protocol object with header-derived identity.
        return MCPProtocol(self.sub, actor=fallback_actor or "http-agent")

    def new_session(self) -> tuple[str, MCPProtocol]:
        sid = uuid.uuid4().hex
        proto = MCPProtocol(self.sub)
        with self.sessions_lock:
            self.sessions[sid] = proto
        return sid, proto

    def drop_session(self, sid: str) -> None:
        with self.sessions_lock:
            self.sessions.pop(sid, None)


class Handler(BaseHTTPRequestHandler):
    server: SubstrateHTTPServer
    server_version = f"substrate/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------------
    def log_message(self, fmt, *args):  # quiet by default; set SUBSTRATE_HTTP_LOG=1 to see
        if os.environ.get("SUBSTRATE_HTTP_LOG"):
            super().log_message(fmt, *args)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json",
              extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), extra=extra)

    def _authorized(self) -> bool:
        token = self.server.token
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        qs = parse_qs(urlparse(self.path).query)
        return qs.get("token", [None])[0] == token

    def _same_origin(self) -> bool:
        """CSRF guard for browser-driven state changes."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            return urlparse(origin).netloc == host
        return True

    # -- routing ------------------------------------------------------------------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {
                "status": "ok", "version": __version__, "workspace": self.server.sub.workspace,
                "uptime_s": int(time.time() - self.server.started), "tools": len(TOOL_SCHEMAS),
                "vector_backend": self.server.sub.files.stats()["vector_backend"],
                "embedder": self.server.sub.memory.embedder.name,
                "sessions": len(self.server.sessions),
            })
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if path == "/mcp":
            return self._json(405, {"error": "server-initiated streams are not supported; POST JSON-RPC"},
                              extra={"Allow": "POST, DELETE"})
        if path in ("/", "/approvals"):
            return self._send(200, self._approvals_page().encode(), "text/html; charset=utf-8")
        if path == "/api/approvals":
            return self._json(200, {"pending": self.server.sub.approvals.pending()})
        if path == "/api/audit":
            rows = self.server.sub.db.conn.execute(
                "SELECT * FROM audit ORDER BY ts DESC LIMIT 200").fetchall()
            return self._json(200, {"audit": [dict(r) for r in rows]})
        if path == "/api/brief":
            return self._json(200, self.server.sub.brief())
        return self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        if urlparse(self.path).path != "/mcp":
            return self._json(404, {"error": "not found"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        sid = self.headers.get("Mcp-Session-Id")
        if sid:
            self.server.drop_session(sid)
        self._send(204)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        if path == "/mcp":
            return self._mcp()
        if path.startswith("/approvals/"):
            return self._approval_action(path)
        return self._json(404, {"error": "not found"})

    # -- MCP ----------------------------------------------------------------------
    def _mcp(self) -> None:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            return self._json(415, {"error": "Content-Type must be application/json"})
        try:
            payload = json.loads(self._body() or b"null")
        except json.JSONDecodeError:
            return self._json(400, {"jsonrpc": "2.0", "id": None,
                                    "error": {"code": -32700, "message": "parse error"}})
        sid = self.headers.get("Mcp-Session-Id")
        proto = self.server.session(sid, self.headers.get("X-Substrate-Agent"))
        extra: dict = {}

        def one(req: dict):
            nonlocal proto, sid, extra
            if not isinstance(req, dict):
                return {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "invalid request"}}
            if req.get("method") == "initialize":
                sid, proto = self.server.new_session()
                extra["Mcp-Session-Id"] = sid
            try:
                return proto.handle(req)
            except Exception as e:  # never 500 on a single bad call
                return {"jsonrpc": "2.0", "id": req.get("id"),
                        "error": {"code": -32603, "message": f"internal error: {e}"}}

        if isinstance(payload, list):
            responses = [r for r in (one(req) for req in payload) if r is not None]
            if not responses:
                return self._send(202, extra=extra)
            return self._json(200, responses, extra=extra)
        resp = one(payload)
        if resp is None:
            return self._send(202, extra=extra)
        self._json(200, resp, extra=extra)

    # -- approvals UI ---------------------------------------------------------------
    def _approval_action(self, path: str) -> None:
        if not self._same_origin():
            return self._json(403, {"error": "cross-origin request rejected"})
        parts = path.strip("/").split("/")
        if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in ("approve", "deny"):
            return self._json(404, {"error": "not found"})
        aid = int(parts[1])
        form = parse_qs(self._body().decode(errors="replace"))
        run = form.get("run", ["0"])[0] == "1"
        sub = self.server.sub
        decided = sub.dispatch("approvals.decide", {"id": aid, "approved": parts[2] == "approve"},
                               actor="human@approvals-ui")
        result = {"decided": decided.get("decided", False)}
        if parts[2] == "approve" and run and result["decided"]:
            result["run"] = sub.dispatch("exec.run_approved", {"approval_id": aid},
                                         actor="human@approvals-ui")
        accept = self.headers.get("Accept", "")
        if "text/html" in accept:
            token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
            loc = "/approvals" + (f"?token={token}" if token else "")
            return self._send(303, extra={"Location": loc})
        self._json(200, result)

    def _approvals_page(self) -> str:
        sub = self.server.sub
        token = parse_qs(urlparse(self.path).query).get("token", [None])[0]
        q = f"?token={html.escape(token)}" if token else ""
        pending = sub.approvals.pending()
        recent = [a for a in sub.approvals.recent(30) if a["status"] != "pending"]

        def row(a: dict, actions: bool) -> str:
            cmd = html.escape(a["command"])
            reason = html.escape(a.get("reason") or "")
            who = html.escape(a.get("requested_by") or "agent")
            cwd = html.escape(a.get("cwd") or "")
            age = int(time.time() - a["created_at"])
            act = ""
            if actions:
                act = (
                    f'<form method="post" action="/approvals/{a["id"]}/approve{q}">'
                    f'<input type="hidden" name="run" value="1"><button class="ok">Approve &amp; run</button></form>'
                    f'<form method="post" action="/approvals/{a["id"]}/approve{q}"><button>Approve</button></form>'
                    f'<form method="post" action="/approvals/{a["id"]}/deny{q}"><button class="no">Deny</button></form>'
                )
            status = html.escape(a["status"])
            return (
                f'<tr><td class="id">#{a["id"]}</td><td><code>{cmd}</code>'
                f'<div class="meta">{reason}</div><div class="meta">from <b>{who}</b>'
                f'{" in " + cwd if cwd else ""} · {age}s ago · <span class="st {status}">{status}</span></div></td>'
                f'<td class="act">{act}</td></tr>'
            )

        body_pending = "".join(row(a, True) for a in pending) or \
            '<tr><td colspan="3" class="empty">Nothing waiting. Agents can keep working.</td></tr>'
        body_recent = "".join(row(a, False) for a in recent)
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Substrate approvals</title>
<meta http-equiv="refresh" content="5">
<style>
:root{{--bg:#0f1115;--fg:#e6e6e6;--mut:#9aa0a6;--card:#171a21;--ok:#2ea043;--no:#d1242f;--acc:#6ca0ff}}
@media (prefers-color-scheme: light){{:root{{--bg:#fafafa;--fg:#111;--mut:#555;--card:#fff}}}}
body{{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;max-width:980px;margin-inline:auto}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:var(--mut);margin-bottom:20px}}
h2{{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:24px 0 8px}}
table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}}
td{{padding:12px;border-top:1px solid rgba(128,128,128,.15);vertical-align:top}}
td.id{{color:var(--mut);width:48px}} td.act{{width:260px}} td.empty{{color:var(--mut);text-align:center}}
code{{font:13px/1.4 ui-monospace,Menlo,Consolas,monospace;word-break:break-all}}
.meta{{color:var(--mut);font-size:13px;margin-top:4px}}
form{{display:inline-block;margin:2px 4px 2px 0}}
button{{border:1px solid rgba(128,128,128,.35);background:transparent;color:var(--fg);padding:6px 10px;border-radius:6px;cursor:pointer}}
button.ok{{background:var(--ok);border-color:var(--ok);color:#fff}} button.no{{color:var(--no);border-color:var(--no)}}
.st{{font-weight:600}} .st.approved,.st.executed{{color:var(--ok)}} .st.denied,.st.expired{{color:var(--no)}}
</style></head><body>
<h1>Substrate approvals <span class="sub">— workspace <b>{html.escape(sub.workspace)}</b></span></h1>
<div class="sub">Commands the safety gate classified as <b>NEEDS_CONFIRMATION</b>. Nothing here runs until you say so. Page auto-refreshes.</div>
<h2>Pending ({len(pending)})</h2>
<table>{body_pending}</table>
<h2>Recent decisions</h2>
<table>{body_recent or '<tr><td class="empty">No decisions yet.</td></tr>'}</table>
</body></html>"""


def serve(substrate: Substrate, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          token: str | None = None) -> SubstrateHTTPServer:
    """Create (but do not start) the server. Call ``serve_forever()`` on the result."""
    return SubstrateHTTPServer((host, port), substrate, token=token)
