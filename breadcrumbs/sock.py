"""sock: the core over a Unix socket or loopback TCP. One JSON line in, one JSON line out; one connection carries many
requests (the framing of hive.py).

    {"id": 1, "verb": "find", "params": {"q": "kettle"}, "harness": "codex", "k": "<token>"}
    {"id": 1, "ok": true, "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "not_found", "message": "..."}}

A Unix socket has mode 0600, so only the user can connect, and it takes no token. TCP binds 127.0.0.1 and wants the
token (core.token, mode 600, next to the default store) in "k", because any local program can connect to a port.
"""
import hmac
import json
import os
import socket
import socketserver
import stat

from .core import CoreError, identify

MAX_LINE = 64 * 1024


def default_path(core):
    return os.path.join(core.stores.base, "core.sock")


def handle_line(core, line, key=None):
    """The reply (a dict) to one request line. key: the token that a request must carry (None: no token)."""
    try:
        req = json.loads(line)
        if not isinstance(req, dict):
            raise ValueError("a request is an object")
    except ValueError as e:
        return {"id": None, "ok": False, "error": {"code": "bad_request", "message": str(e)}}
    rid = req.get("id")
    if key is not None and not hmac.compare_digest(str(req.get("k", "")), key):
        return {"id": rid, "ok": False, "error": {"code": "token", "message": "bad or missing token"}}
    try:
        harness = identify(req["harness"]) if isinstance(req.get("harness"), str) else None
        return {"id": rid, "ok": True, "result": core.call(req.get("verb"), req.get("params"), harness)}
    except CoreError as e:
        return {"id": rid, "ok": False, "error": {"code": e.code, "message": e.message}}
    except Exception as e:  # a request never ends the server
        return {"id": rid, "ok": False, "error": {"code": "internal", "message": "%s: %s" % (type(e).__name__, e)}}


def make_handler(core, key):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                line = self.rfile.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line) > MAX_LINE:
                    out = {"id": None, "ok": False, "error": {"code": "bad_request", "message": "line too long"}}
                else:
                    out = handle_line(core, line, key)
                self.wfile.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
    return Handler


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def make_tcp(core, port, key):
    return TCPServer(("127.0.0.1", port), make_handler(core, key))


def live(path):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.settimeout(1)
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def make_unix(core, path):
    """A Unix socket server at path. A stale socket file is replaced; a live one is an error."""
    if os.path.exists(path):
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            raise OSError("%s is not a socket" % path)
        if live(path):
            raise OSError("%s: a core already answers there" % path)
        os.unlink(path)
    old = os.umask(0o177)  # the socket is born 0600
    try:
        server = UnixServer(path, make_handler(core, None))
    finally:
        os.umask(old)
    return server


def request(address, verb, params=None, harness=None, key=None, timeout=10):
    """One call, as a client: address is a Unix socket path, or (host, port). Returns the whole reply."""
    s = socket.socket(socket.AF_UNIX if isinstance(address, str) else socket.AF_INET)
    try:
        s.settimeout(timeout)
        s.connect(address)
        msg = {"id": 1, "verb": verb, "params": params or {}}
        if harness:
            msg["harness"] = harness
        if key:
            msg["k"] = key
        s.sendall(json.dumps(msg).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf)
    finally:
        s.close()
