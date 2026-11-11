"""core: one verb table behind MCP (stdio), a socket (Unix, TCP) and A2A. The transports are thin; the cases below
check the core once and each transport's framing once."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import a2a, core, mcp, sock
from breadcrumbs.hermes import BreadcrumbsProvider

TEA = ("how long should green tea steep in the kettle water", "steep green tea two minutes; kettle water near 80 C")
BIKE = ("my bike chain skips on the small cog", "replace the worn chain and check every cog for wear")


def make_store(hermes_home, sid, turn):
    p = BreadcrumbsProvider()
    p.initialize(sid, hermes_home=hermes_home, platform="cli", agent_context="primary")
    p.sync_turn(*turn, session_id=sid)
    ref = p.session_crumb(sid)
    p.shutdown()
    return ref


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after the core closes its Views
        self.tea = make_store(self.tmp.name, "tea", TEA)
        self.bike = make_store(os.path.join(self.tmp.name, "profiles", "alpha"), "bike", BIKE)
        self.core = core.Core(core.Stores(root=self.tmp.name))
        self.addCleanup(self.core.close)


class Verbs(CoreCase):
    def test_the_harnesses_are_told_apart_by_their_names(self):
        names = {"claude-code": "claude-code", "codex-mcp-client": "codex", "opencode": "opencode",
                 "Hermes Agent": "hermes", "curl": "unknown", None: "unknown"}
        for name, key in names.items():
            self.assertEqual(core.identify(name), key, name)

    def test_the_stores_are_the_default_and_each_profile(self):
        out = self.core.call("stores")["stores"]
        self.assertEqual([(s["name"], s["present"]) for s in out], [("default", True), ("alpha", True)])

    def test_find_looks_in_all_stores_and_names_the_store_of_each_group(self):
        both = self.core.call("find", {"q": "tea kettle bike chain"})["groups"]
        self.assertEqual({g["store"] for g in both}, {"default", "alpha"})
        one = self.core.call("find", {"q": "bike chain cog", "store": "alpha"})["groups"]
        self.assertEqual({g["store"] for g in one}, {"alpha"})

    def test_open_finds_a_crumb_in_any_store_and_zoom_is_shorter(self):
        full = self.core.call("open", {"id": self.bike})
        self.assertEqual(full["store"], "alpha")
        self.assertIn("worn chain", full["text"])
        part = self.core.call("zoom", {"id": self.bike, "level": 0.0})
        self.assertLessEqual(len(part["text"]), len(full["text"]))

    def test_a_refusal_has_a_code(self):
        for verb, params, code in (("open", {"id": "ffffffff"}, "not_found"), ("nope", {}, "unknown_verb"),
                                   ("find", {}, "bad_params"), ("find", {"q": 3}, "bad_params"),
                                   ("find", {"q": "x", "store": "zzz"}, "no_store"), ("health", {"x": 1}, "bad_params")):
            with self.assertRaises(core.CoreError) as c:
                self.core.call(verb, params)
            self.assertEqual(c.exception.code, code, verb)

    def test_health_counts_the_calls_of_each_harness(self):
        self.core.call("stores", harness="codex")
        self.core.call("stores", harness="codex")
        self.core.call("stores", harness="claude-code")
        self.core.call("stores")
        h = self.core.call("health")
        self.assertEqual(h["calls"], {"codex": 2, "claude-code": 1, "unknown": 2})  # the health call counts itself
        self.assertEqual(h["stores"], ["default", "alpha"])

    def test_the_view_stays_open_between_calls(self):
        self.core.call("find", {"q": "tea", "store": "default"})
        first = self.core._views["default"][0]
        self.core.call("find", {"q": "tea", "store": "default"})
        self.assertIs(self.core._views["default"][0], first)

    def test_the_local_store_is_named_local_unless_it_is_the_default(self):
        self.assertIn("local", core.Stores(local=os.path.join(self.tmp.name, "profiles", "alpha", "breadcrumbs"),
                                           root=self.tmp.name).names())
        self.assertNotIn("local", core.Stores(local=os.path.join(self.tmp.name, "breadcrumbs"), root=self.tmp.name).names())


class Mcp(CoreCase):
    def talk(self, session, method, params=None, rid=1):
        return session.handle({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})

    def test_initialize_names_the_harness_and_the_tools_are_the_verbs(self):
        s = mcp.Session(self.core)
        init = self.talk(s, "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex-mcp-client"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "breadcrumbs")
        self.assertIsNone(s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = self.talk(s, "tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], list(core.VERBS))
        self.assertEqual(tools[0]["inputSchema"]["required"], ["q"])
        self.talk(s, "tools/call", {"name": "stores"})
        self.assertEqual(self.core.call("health")["calls"], {"codex": 1, "unknown": 1})

    def test_open_returns_the_text_and_a_refusal_is_a_tool_error(self):
        s = mcp.Session(self.core)
        ok = self.talk(s, "tools/call", {"name": "open", "arguments": {"id": self.tea}})["result"]
        self.assertFalse(ok["isError"])
        self.assertIn("kettle", ok["content"][0]["text"])
        bad = self.talk(s, "tools/call", {"name": "open", "arguments": {"id": "ffffffff"}})["result"]
        self.assertTrue(bad["isError"])
        self.assertEqual(self.talk(s, "nope")["error"]["code"], -32601)

    def test_serve_reads_lines_and_writes_one_line_each(self):
        import io
        out = io.StringIO()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), "{bad", ""]
        mcp.serve(self.core, io.StringIO("\n".join(lines)), out)
        replies = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("result", r.get("error", {}).get("code")) for r in replies], [{}, -32700])


class Socket(CoreCase):
    def test_a_line_in_a_line_out_and_the_token_guards_tcp(self):
        line = json.dumps({"id": 7, "verb": "stores", "harness": "opencode"})
        self.assertTrue(sock.handle_line(self.core, line)["ok"])
        self.assertEqual(self.core.call("health")["calls"], {"opencode": 1, "unknown": 1})
        self.assertEqual(sock.handle_line(self.core, line, key="secret")["error"]["code"], "token")
        self.assertTrue(sock.handle_line(self.core, line[:-1] + ', "k": "secret"}', key="secret")["ok"])
        self.assertEqual(sock.handle_line(self.core, "[1]")["error"]["code"], "bad_request")

    def test_a_unix_socket_has_mode_0600_and_answers_many_requests(self):
        d = tempfile.mkdtemp(dir="/tmp")  # a short path: the limit of a socket path is 104 bytes
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "c.sock")
        server = sock.make_unix(self.core, path)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertTrue(sock.request(path, "stores")["ok"])
        r = sock.request(path, "open", {"id": self.tea})
        self.assertIn("kettle", r["result"]["text"])
        with self.assertRaises(OSError):  # a live socket is not replaced
            sock.make_unix(self.core, path)

    def test_tcp_wants_the_token(self):
        server = sock.make_tcp(self.core, 0, "secret")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        addr = ("127.0.0.1", server.server_address[1])
        self.assertEqual(sock.request(addr, "stores")["error"]["code"], "token")
        self.assertTrue(sock.request(addr, "stores", key="secret")["ok"])


class A2a(CoreCase):
    def send(self, parts, **meta):
        msg = {"kind": "message", "role": "user", "messageId": "m", "parts": parts}
        if meta:
            msg["metadata"] = meta
        return a2a.rpc(self.core, {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": msg}})

    def test_a_text_message_and_a_data_message_complete_a_task(self):
        t = self.send([{"kind": "text", "text": "find bike chain"}], harness="claude-code")["result"]
        self.assertEqual(t["status"]["state"], "completed")
        self.assertEqual(t["artifacts"][0]["parts"][0]["data"]["groups"][0]["store"], "alpha")
        d = self.send([{"kind": "data", "data": {"verb": "open", "params": {"id": self.tea}}}])["result"]
        self.assertIn("kettle", d["artifacts"][0]["parts"][0]["data"]["text"])
        self.assertEqual(self.core.call("health")["calls"], {"claude-code": 1, "unknown": 2})

    def test_a_refusal_is_a_failed_task_and_a_bad_request_is_an_rpc_error(self):
        t = self.send([{"kind": "text", "text": "open ffffffff"}])["result"]
        self.assertEqual(t["status"]["state"], "failed")
        self.assertIn("not_found", t["status"]["message"]["parts"][0]["text"])
        self.assertEqual(a2a.rpc(self.core, {"jsonrpc": "2.0", "id": 1, "method": "message/send"})["error"]["code"], -32602)

    def test_the_card_lists_the_verbs_and_the_post_wants_the_bearer_token(self):
        server = a2a.make_server(self.core, 0, "secret")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d" % server.server_address[1]
        with urllib.request.urlopen(base + "/.well-known/agent-card.json") as r:
            self.assertEqual([s["id"] for s in json.load(r)["skills"]], list(core.VERBS))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {
            "parts": [{"kind": "text", "text": "stores"}]}}}).encode()
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/", body))
        e.exception.close()
        self.assertEqual(e.exception.code, 401)
        req = urllib.request.Request(base + "/", body, {"Authorization": "Bearer secret", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(json.load(r)["result"]["status"]["state"], "completed")


if __name__ == "__main__":
    unittest.main()
