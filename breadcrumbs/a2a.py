"""a2a: the core as an A2A agent over HTTP on 127.0.0.1 (A2A protocol 0.3, JSON-RPC binding).

    GET  /.well-known/agent-card.json   the agent card: one skill for each verb (no token)
    POST /                              JSON-RPC 2.0: message/send     (header Authorization: Bearer <core.token>)

A message holds either a data part {"verb": "find", "params": {"q": "kettle"}}, or a text part: "find kettle",
"open <id>", "zoom <id> 0.5", "trail", "stores", "health". The reply is a completed Task whose artifact holds the
result as a data part, or a failed Task whose status message says why. The core finishes each call at once, so
tasks/get has nothing to find, and there is no streaming. The harness is the "harness" key of the message's
metadata, else the X-Harness header, else the User-Agent.
"""
import hmac
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import VERBS, VERSION, CoreError, identify

PROTOCOL = "0.3.0"
MAX_BODY = 1 << 20


def card(url):
    return {
        "protocolVersion": PROTOCOL, "name": "breadcrumbs", "version": VERSION, "url": url,
        "preferredTransport": "JSONRPC",
        "description": "Content-addressed breadcrumb memory: find, open and zoom the crumbs of the stores on this machine (read-only).",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}],
        "defaultInputModes": ["text/plain", "application/json"], "defaultOutputModes": ["application/json"],
        "skills": [{"id": v, "name": v, "description": VERBS[v]["doc"], "tags": ["memory", "read-only"]} for v in VERBS],
    }


def parse(message):
    """(verb, params) from the parts of a message."""
    parts = [p for p in (message.get("parts") or []) if isinstance(p, dict)]
    for p in parts:
        if p.get("kind") == "data" and isinstance(p.get("data"), dict):
            return p["data"].get("verb"), p["data"].get("params") or {}
    text = " ".join(p.get("text", "") for p in parts if p.get("kind") == "text").strip()
    verb, _, rest = text.partition(" ")
    rest = rest.strip()
    if verb == "find":
        return verb, {"q": rest}
    if verb in ("open", "zoom"):
        ref, _, level = rest.partition(" ")
        params: dict = {"id": ref}
        if verb == "zoom" and level.strip():
            try:
                params["level"] = float(level)
            except ValueError:
                raise CoreError("bad_params", "zoom takes a level 0..1")
        return verb, params
    return verb, {}


def task(state, verb, result=None, why=None):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = {"kind": "task", "id": uuid.uuid4().hex, "contextId": uuid.uuid4().hex, "status": {"state": state, "timestamp": now}}
    if why:
        out["status"]["message"] = {"kind": "message", "role": "agent", "messageId": uuid.uuid4().hex,
                                    "parts": [{"kind": "text", "text": why}]}
    if result is not None:
        out["artifacts"] = [{"artifactId": uuid.uuid4().hex, "name": verb, "parts": [{"kind": "data", "data": result}]}]
    return out


def rpc(core, req, header_harness=""):
    """The JSON-RPC reply (a dict) to one request."""
    rid = req.get("id") if isinstance(req, dict) else None

    def fail(code, message):
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
        return fail(-32600, "not a JSON-RPC 2.0 request")
    if req.get("method") != "message/send":
        code = -32001 if req.get("method") == "tasks/get" else -32601
        return fail(code, "task not found: the core answers at once" if code == -32001 else "method not found")
    message = (req.get("params") or {}).get("message")
    if not isinstance(message, dict):
        return fail(-32602, "params.message is missing")
    meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    harness = identify(meta.get("harness") or header_harness)
    try:
        verb, params = parse(message)
        return {"jsonrpc": "2.0", "id": rid, "result": task("completed", verb, core.call(verb, params, harness))}
    except CoreError as e:
        return {"jsonrpc": "2.0", "id": rid, "result": task("failed", "", why="%s: %s" % (e.code, e.message))}


def make_server(core, port, key):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def url(self):
            return "http://127.0.0.1:%d/" % self.server.server_address[1]

        def do_GET(self):
            if self.path.split("?")[0] in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                return self._send(200, card(self.url()))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key):
                return self._send(401, {"error": "bearer token"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > MAX_BODY:
                    return self._send(413, {"error": "too large"})
                req = json.loads(self.rfile.read(size))
            except ValueError:
                return self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            self._send(200, rpc(core, req, self.headers.get("X-Harness") or self.headers.get("User-Agent") or ""))

        def log_message(self, format, *args):  # noqa: A002 (the base class name)
            pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True

    return Server(("127.0.0.1", port), Handler)
