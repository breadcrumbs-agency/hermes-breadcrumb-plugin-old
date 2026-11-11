"""mcp: the core as an MCP server over stdio. The harness starts it as a child and speaks JSON-RPC 2.0, one message
for each line (the MCP stdio transport). The tools are the verbs of core.py, with their names as they are; a client
shows them under the server's name (breadcrumbs). stdout carries the protocol and nothing else.

The client's name (initialize, clientInfo.name) tells the harness apart: claude-code, codex, opencode, hermes.
"""
import json
import sys

from .core import VERBS, VERSION, CoreError, identify, tool_schema

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


class Session:
    def __init__(self, core):
        self.core, self.harness = core, None

    def result(self, verb, args):
        """A tools/call result. A refusal of the core is a tool error the model can read, not a protocol error."""
        try:
            out = self.core.call(verb, args, self.harness)
            text = out["text"] if verb in ("open", "zoom") else json.dumps(out, ensure_ascii=False)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except CoreError as e:
            return {"content": [{"type": "text", "text": "%s: %s" % (e.code, e.message)}], "isError": True}

    def handle(self, msg):
        """The reply (a dict) to one message, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "not a JSON-RPC 2.0 request"}}
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if "id" not in msg:  # a notification (notifications/initialized, notifications/cancelled): no reply
            return None

        def ok(result):
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        def fail(code, message):
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

        if method == "initialize":
            self.harness = identify((params.get("clientInfo") or {}).get("name"))
            asked = params.get("protocolVersion")
            return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "breadcrumbs", "version": VERSION}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": [{"name": v, "description": VERBS[v]["doc"], "inputSchema": tool_schema(v),
                                  "annotations": {"readOnlyHint": True}} for v in VERBS]})
        if method == "tools/call":
            if params.get("name") not in VERBS:
                return fail(-32602, "unknown tool %r" % params.get("name"))
            return ok(self.result(params["name"], params.get("arguments") or {}))
        return fail(-32601, "method not found: %s" % method)


def serve(core, stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    session = Session(core)
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = session.handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
    return 0
