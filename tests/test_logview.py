"""The log views (breadcrumbs/logview.py and the /log/* routes of dashboard/plugin_api.py), tested on a state.db that
Hermes's own SessionDB writes. No breadcrumbs.

The log has fixed times and neutral text: two earlier sessions (a bike, a balcony garden) and the
session "today". In "today", a provider injects context into three user rows (api_content), the
model calls session_search in scroll shape four times (and once in query shape), the user takes one
turn back (rewind_to_message), and Hermes's own archive_and_compact compacts the session: it keeps a
head, adds one summary row (_compressed_summary) and carries the tail.

The Gantt of "today" is also tests/fixtures/log-context.json, so that a change of the log Gantt is seen. Set
SPACEGRAPH_WRITE_FIXTURES=1 to write that file again after an intended change. One test compares the
context with the TypeScript interfaces of the drawing code (spacegraph/src/gantt.ts); that folder is not in the
release, so the test runs only where the file exists.
Run with the Python of a Hermes install (it has hermes_state and FastAPI); without them the suite skips.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SG = ROOT / "spacegraph"  # the drawing code of the Desktop page (not in the release; its TypeScript interfaces)
sys.path.insert(0, str(ROOT))

from breadcrumbs import logview  # noqa: E402

try:
    from hermes_state import SessionDB
except ImportError:  # not the Python of a Hermes install
    SessionDB = None

PREFIX = "/api/plugins/breadcrumbs"
FIXTURE = ROOT / "tests" / "fixtures" / "log-context.json"
T0 = 1_794_000_000.0  # a fixed clock
BIKE = [("user", "my bike chain skips on the small cog"),
        ("assistant", "Replace the worn chain first, then check the cog teeth."),
        ("user", "how do I measure chain wear"),
        ("assistant", "Use a chain checker: 0.75 percent stretch means a new chain.")]
GARDEN = [("user", "when should I plant tomato seedlings outside"),
          ("assistant", "After the last frost, when the nights stay above 10 C.")]
INJECTED = ["Notes from a memory provider: chain cleaning came up before.",
            "Notes from a memory provider: an earlier talk about chain wear.",
            "Notes from a memory provider: the noise came back after rain."]
SUMMARY = "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns: a noisy chain, and how to measure its wear."
EVENT_KEYS = ["id", "ev", "turn", "ts", "bytes", "budget", "ovf", "rate", "band", "q", "turn_budget", "used", "cap",
              "want"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utf8(text):
    return len(text.encode("utf-8"))


def call(call_id, **args):
    return {"id": call_id, "type": "function", "function": {"name": "session_search", "arguments": json.dumps(args)}}


def scroll_result(sid, anchor, rows):
    """A tool result in the shape of Hermes's session_search scroll (fixed, so the fixture is too)."""
    return json.dumps({"success": True, "mode": "scroll", "session_id": sid, "around_message_id": anchor, "window": 1,
                       "messages": [{"id": i, "role": role, "content": text} for i, role, text in rows]})


def build(top):
    """The test log in top/state.db; returns its path, the row ids by name, the tool results by call
    id, and the answer of Hermes's real session_search to each scroll call (when it is importable)."""
    path = Path(top) / "state.db"
    assert SessionDB is not None
    try:
        from tools.session_search_tool import session_search
    except ImportError:
        session_search = None
    db = SessionDB(db_path=path)
    ids, results, real = {}, {}, {}
    clock = iter(T0 + 60 * n for n in range(1, 1000))
    started = {}

    def add(sid, key, role, text, **kw):
        ids[key] = db.append_message(sid, role, text, timestamp=next(clock), **kw)
        return ids[key]

    def scroll(key, call_id, sid, anchor, rows):
        if session_search is not None:  # the real tool accepts the call, as the model makes it
            real[call_id] = json.loads(session_search(session_id=sid, around_message_id=anchor, window=1, db=db,
                                                      current_session_id="s-today"))
        results[call_id] = scroll_result(sid, anchor, rows)
        add("s-today", key, "tool", results[call_id], tool_name="session_search", tool_call_id=call_id)

    try:
        for sid, title, rows in (("s-bike", "Bike repair", BIKE), ("s-garden", "Balcony garden", GARDEN)):
            db.create_session(sid, "cli", model="test")
            db.set_session_title(sid, title)
            started[sid] = next(clock)
            for i, (role, text) in enumerate(rows):
                add(sid, "%s:%d" % (sid, i), role, text)
        db.create_session("s-today", "cli", model="test")
        db.set_session_title("s-today", "Today")
        started["s-today"] = next(clock)
        u0 = "the chain on my bike is noisy again"
        add("s-today", "u0", "user", u0, api_content=u0 + "\n\n" + INJECTED[0])
        add("s-today", "a0", "assistant", "Clean the chain, oil it, and listen again.")
        u1 = "what did we find about chain wear last time"
        add("s-today", "u1", "user", u1, api_content=u1 + "\n\n" + INJECTED[1])
        b2, b3 = ids["s-bike:2"], ids["s-bike:3"]
        add("s-today", "a1", "assistant", "", tool_calls=[call("call_t1", session_id="s-bike", around_message_id=b3,
                                                                window=1)])
        scroll("tool1", "call_t1", "s-bike", b3, [(b2, "user", BIKE[2][1]), (b3, "assistant", BIKE[3][1])])
        add("s-today", "a1b", "assistant", "Last time: a chain checker, and 0.75 percent stretch means a new chain.")
        add("s-today", "ux", "user", "never mind that")
        add("s-today", "ax", "assistant", "Okay.")
        db.rewind_to_message("s-today", ids["ux"])  # the user takes the turn back: active 0, compacted 0
        add("s-today", "u2", "user", "does the cassette wear out too")
        b0, b1 = ids["s-bike:0"], ids["s-bike:1"]
        add("s-today", "a2", "assistant", "", tool_calls=[call("call_t2a", query="cassette wear"),
                                                          call("call_t2b", session_id="s-bike", around_message_id=b0,
                                                               window=1)])
        results["call_t2a"] = json.dumps({"success": True, "mode": "discover", "results": []})
        add("s-today", "tool2a", "tool", results["call_t2a"], tool_name="session_search", tool_call_id="call_t2a")
        scroll("tool2b", "call_t2b", "s-bike", b0, [(b0, "user", BIKE[0][1]), (b1, "assistant", BIKE[1][1])])
        add("s-today", "a2b", "assistant", "Yes: a worn cassette skips with a new chain.")
        # Hermes's in-place compaction: the head and the tail as copies, one summary row between them.
        live = db.get_messages("s-today")
        summary = {"role": "user", "content": SUMMARY, "_compressed_summary": True, "timestamp": next(clock)}
        db.archive_and_compact("s-today", live[:2] + [summary] + live[-5:], tail_count=5)
        for key, row in zip(("u0c", "a0c", "summary", "u2c", "a2c", "tool2ac", "tool2bc", "a2bc"),
                            db.get_messages("s-today")):
            ids[key] = row["id"]
        u3 = "back to the noise: the chain or the cassette"
        add("s-today", "u3", "user", u3, api_content=u3 + "\n\n" + INJECTED[2])
        g0, g1 = ids["s-garden:0"], ids["s-garden:1"]
        a1b = ids["a1b"]
        add("s-today", "a3", "assistant", "", tool_calls=[
            call("call_t3a", session_id="s-today", around_message_id=a1b, window=1),
            call("call_t3b", session_id="s-garden", around_message_id=g1, window=1)])
        scroll("tool3a", "call_t3a", "s-today", a1b, [(ids["tool1"], "tool", "…"), (a1b, "assistant", "…")])
        scroll("tool3b", "call_t3b", "s-garden", g1, [(g0, "user", GARDEN[0][1]), (g1, "assistant", GARDEN[1][1])])
        add("s-today", "a3b", "assistant", "The chain: clean and oil it first, then check its wear.")
        db.create_session("s-empty", "cli", model="test")
        started["s-empty"] = next(clock)
    finally:
        db.close()
    conn = sqlite3.connect(path)  # SessionDB has no started_at parameter: fix the session clocks here
    try:
        conn.executemany("UPDATE sessions SET started_at = ? WHERE id = ?", [(t, s) for s, t in started.items()])
        conn.commit()
    finally:
        conn.close()
    return path, ids, results, real


def plugin_client(home, hermes, extra_file=None):
    """The plugin router on a bare FastAPI app, pointed at a scratch Hermes root."""
    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_breadcrumbs_logtest",
                                                  ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = api
    spec.loader.exec_module(api)
    setattr(api, "active_home", lambda: Path(home))  # setattr: the test points the module at a scratch root
    setattr(api, "hermes_root", lambda: Path(hermes))
    setattr(api, "EXTRA_FILE", extra_file or Path(hermes) / "no-stores.local.json")  # never the repo's file
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return api, TestClient(app)


def files(top):
    """The files under top (SQLite's -wal and -shm aside): path -> (size, sha256)."""
    out = {}
    for base, _, names in os.walk(top):
        for n in names:
            if not n.endswith(("-wal", "-shm")):
                p = Path(base) / n
                out[str(p.relative_to(top))] = (p.stat().st_size, sha(p))
    return out


@unittest.skipIf(SessionDB is None, "needs the Python of a Hermes install (hermes_state)")
class LogViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="sg-logview-")
        top = Path(cls.tmp.name)
        built, cls.ids, cls.results, cls.real = build(top / "build")
        cls.hermes = top / "hermes"
        cls.home = cls.hermes / "profiles" / "demo"
        cls.home.mkdir(parents=True)
        cls.db = cls.home / "state.db"
        shutil.copy2(built, cls.db)  # the closed file: SessionDB never opens the scratch Hermes root
        cls.digest = sha(cls.db)
        cls.ctx = logview.context(cls.db, "s-today")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def rid(self, key):  # a row id of the log, by its name in build()
        return str(self.ids[key])

    def row(self, key):
        return next(r for r in self.ctx["rows"] if r["id"] == self.rid(key))

    def events(self, ev):
        return [e for e in self.ctx["events"] if e["ev"] == ev]


class ShapeTest(LogViewTest):
    def interface(self, name):
        src = (SG / "src" / "gantt.ts").read_text()
        body = re.search(r"export interface %s \{(.*?)\n\}" % name, src, re.S)
        self.assertIsNotNone(body, name)
        return set(re.findall(r"^\s+(\w+)\??:", body.group(1) if body else "", re.M))

    @unittest.skipUnless((SG / "src" / "gantt.ts").is_file(), "spacegraph/src/gantt.ts is not in the release")
    def test_the_context_has_exactly_the_shape_of_gantt_ts(self):
        c = self.ctx
        self.assertEqual(set(c), self.interface("Context"))
        self.assertEqual(set(EVENT_KEYS), self.interface("ContextEvent"))
        for e in c["events"]:
            self.assertEqual(list(e), EVENT_KEYS)
        for t in c["turns"]:
            self.assertEqual(set(t), self.interface("ContextTurn"))
        for r in c["rows"]:
            self.assertEqual(set(r), self.interface("ContextRow"))
            for b in r["bars"]:
                self.assertEqual(set(b), self.interface("ContextBar"))
        self.assertEqual(json.loads(json.dumps(c)), c)  # plain JSON values only

    def test_the_route_shapes_of_session_and_sessions(self):
        s = logview.session(self.db, "s-today")
        self.assertEqual(set(s), {"session", "root", "nodes", "edges"})
        for n in s["nodes"]:
            self.assertEqual(set(n), {"id", "kind", "title", "tokens", "meta"})
            self.assertEqual(set(n["meta"]), {"tool_name", "ts", "compacted", "summary", "turn"})
        for e in s["edges"]:
            self.assertEqual(set(e), {"kind", "source", "target", "w"})
        for item in logview.sessions(self.db)["sessions"]:
            self.assertEqual(set(item), {"id", "title", "started", "messages", "blocks"})


class TurnTest(LogViewTest):
    def test_turns_are_the_user_rows_in_id_order(self):
        # u0 is compacted with a live copy (the head): one turn. ux was taken back. u2 lives as its tail copy.
        self.assertEqual([t["crumb"] for t in self.ctx["turns"]], [self.rid(k) for k in ("u0", "u1", "u2c", "u3")])
        self.assertEqual([t["turn"] for t in self.ctx["turns"]], [0, 1, 2, 3])
        self.assertTrue(all(isinstance(t["ts"], float) for t in self.ctx["turns"]))

    def test_events_are_in_time_order(self):
        evs = self.ctx["events"]
        self.assertEqual([e["ev"] for e in evs], ["recall", "recall", "open", "open", "compress", "recall", "open", "open"])
        self.assertEqual([e["ts"] for e in evs], sorted(e["ts"] for e in evs))
        self.assertEqual([e["turn"] for e in evs], [0, 1, 1, 2, 2, 3, 3, 3])

    def test_each_event_has_the_fixed_fields_and_the_running_used(self):
        evs = self.ctx["events"]
        for i, e in enumerate(evs):
            self.assertEqual((e["budget"], e["rate"], e["band"], e["q"], e["turn_budget"], e["cap"]),
                             (0, 0.0, None, "", None, False), e)
            self.assertEqual(e["want"], e["bytes"], e)
            self.assertEqual(e["ovf"], e["ev"] == "compress", e)
            so_far = sum(x["bytes"] for x in evs[:i + 1] if x["turn"] == e["turn"] and x["ev"] in ("recall", "open"))
            self.assertEqual(e["used"], so_far, e)


class EventTest(LogViewTest):
    def test_recall_is_the_context_that_api_content_adds(self):
        recall = self.events("recall")
        self.assertEqual([e["id"] for e in recall], ["recall-" + self.rid(k) for k in ("u0", "u1", "u3")])
        self.assertEqual([e["bytes"] for e in recall], [utf8("\n\n" + text) for text in INJECTED])
        bars = [b for r in self.ctx["rows"] for b in r["bars"]]
        self.assertFalse([b for b in bars if b["ev"] == "recall"])  # no items: a provider's format is not read

    def test_open_is_a_session_search_scroll_call(self):
        opened = self.events("open")
        self.assertEqual([e["id"] for e in opened],
                         ["open-%s-0" % self.rid("a1"), "open-%s-1" % self.rid("a2c"), "open-%s-0" % self.rid("a3"),
                          "open-%s-1" % self.rid("a3")])  # call_t2a (query shape) is no open
        self.assertEqual([e["bytes"] for e in opened],
                         [utf8(self.results[c]) for c in ("call_t1", "call_t2b", "call_t3a", "call_t3b")])

    def test_hermes_session_search_accepts_each_scroll_call(self):
        if not self.real:
            self.skipTest("tools.session_search_tool is not importable")
        self.assertEqual({c: (r["success"], r["mode"]) for c, r in self.real.items()},
                         {c: (True, "scroll") for c in ("call_t1", "call_t2b", "call_t3a", "call_t3b")})

    def test_the_compaction_is_one_compress_event_at_its_time(self):
        (e,) = self.events("compress")
        self.assertEqual((e["id"], e["bytes"], e["ovf"]), ("compress-" + self.rid("summary"), utf8(SUMMARY), True))
        self.assertEqual(e["turn"], 2)  # after the tail that it kept (u2), not before it as in id order
        self.assertNotIn(self.rid("summary"), [t["crumb"] for t in self.ctx["turns"]])

    def test_the_scroll_parser_follows_hermes_dispatch(self):
        text = json.dumps([
            call("a", session_id="s1", around_message_id=12),
            {"id": "b", "function": {"name": "session_search", "arguments": {"session_id": " s2 ", "around_message_id": "7"}}},
            call("c", query="chain"), call("d", session_id="", around_message_id=3), call("e", session_id="s3"),
            call("f", session_id="s4", around_message_id=True), call("g", session_id="s5", around_message_id="x"),
            {"id": "h", "function": {"name": "terminal", "arguments": "{}"}}, "junk"])
        self.assertEqual(logview.scroll_calls(text), [(0, "a", "s1", 12), (1, "b", "s2", 7)])
        self.assertEqual(logview.scroll_calls("not json session_search"), [])
        self.assertEqual(logview.scroll_calls(""), [])


class RowTest(LogViewTest):
    OWN = ("u0", "a0", "u1", "a1", "tool1", "a1b", "summary", "u2c", "a2c", "tool2ac", "tool2bc", "a2bc",
           "u3", "a3", "tool3a", "tool3b", "a3b")

    def test_rows_are_the_messages_then_the_rows_that_opens_name(self):
        ids = [r["id"] for r in self.ctx["rows"]]
        self.assertEqual(ids, [self.rid(k) for k in self.OWN + ("s-bike:3", "s-bike:0", "s-garden:1")])
        for key in ("ux", "ax", "u2", "a2b", "u0c", "a0c"):  # taken back, carried (rewound) or folded copies
            self.assertNotIn(self.rid(key), ids)
        sessions = {r["id"]: r["session"] for r in self.ctx["rows"]}
        self.assertEqual({sessions[self.rid(k)] for k in self.OWN}, {"s-today"})
        self.assertEqual([sessions[self.rid(k)] for k in ("s-bike:3", "s-bike:0", "s-garden:1")],
                         ["s-bike", "s-bike", "s-garden"])

    def test_a_row_has_a_role_and_a_short_label(self):
        self.assertEqual(self.row("u0")["label"], "user the chain on my bike is noisy again")
        self.assertEqual(self.row("a1")["label"], "assistant ")  # a tool call row has no content
        self.assertEqual(self.row("tool1")["label"], "tool session_search " + self.results["call_t1"][:40])
        self.assertEqual(self.row("s-bike:3")["label"], "assistant " + BIKE[3][1][:40])
        self.assertEqual([self.row(k)["kind"] for k in ("u0", "a1", "tool1", "summary")],
                         ["user", "assistant", "tool", "user"])
        self.assertEqual(logview.label("tool", "terminal", "  a\n\n b\tc  " + "x" * 50), "tool terminal a b c " + "x" * 34)

    def own(self, key):
        (bar,) = [b for b in self.row(key)["bars"] if b["ev"] == "own"]
        return bar

    def test_own_bars_end_at_the_compress_that_took_them_out(self):
        stop = self.ctx["events"].index(self.events("compress")[0])
        for key in ("u1", "a1", "tool1", "a1b"):  # archived by the compaction
            self.assertEqual({k: self.own(key)[k] for k in ("from", "to", "t1", "level")},
                             {"from": None, "to": stop, "t1": 2, "level": 1.0}, key)
        for key in ("u0", "a0", "summary", "u2c", "a2bc", "u3", "a3b"):  # kept (head, tail) or later: live
            self.assertEqual((self.own(key)["to"], self.own(key)["t1"]), (None, None), key)
        self.assertEqual([self.own(k)["t0"] for k in ("u0", "a1b", "summary", "tool2bc", "a3b")], [0, 1, 2, 2, 3])
        self.assertEqual(self.own("u0")["bytes"], utf8("the chain on my bike is noisy again"))  # no injected part
        self.assertGreater(self.own("a1")["bytes"], 0)  # its tool calls are its text

    def test_open_bars_end_where_their_tool_result_leaves_the_context(self):
        evs = self.ctx["events"]
        stop = evs.index(self.events("compress")[0])
        opened = [evs.index(e) for e in self.events("open")]
        bike3, = self.row("s-bike:3")["bars"]
        self.assertEqual(bike3, {"from": opened[0], "to": stop, "t0": 1, "t1": 2, "level": 1.0,
                                 "bytes": utf8(self.results["call_t1"]), "ev": "open"})  # tool1 was archived
        bike0, = self.row("s-bike:0")["bars"]
        self.assertEqual((bike0["from"], bike0["to"], bike0["t1"]), (opened[1], None, None))  # its tool row lives on
        self.assertEqual([b["ev"] for b in self.row("a1b")["bars"]], ["own", "open"])  # a message keeps its row
        self.assertEqual(self.row("a1b")["bars"][1]["from"], opened[2])
        garden, = self.row("s-garden:1")["bars"]
        self.assertEqual((garden["from"], garden["t0"], garden["bytes"]), (opened[3], 3, utf8(self.results["call_t3b"])))


class SessionTest(LogViewTest):
    def setUp(self):
        self.s = logview.session(self.db, "s-today")

    def test_nodes_are_the_messages_of_the_log(self):
        ctx_own = [r["id"] for r in self.ctx["rows"] if r["session"] == "s-today"]
        self.assertEqual([n["id"] for n in self.s["nodes"]], ctx_own)
        self.assertEqual(self.s["root"], self.rid("u0"))
        node = {n["id"]: n for n in self.s["nodes"]}
        u0 = node[self.rid("u0")]
        self.assertEqual((u0["kind"], u0["title"], u0["tokens"]), ("user", self.row("u0")["label"],
                                                                     len("the chain on my bike is noisy again") // 4))
        self.assertEqual(node[self.rid("tool1")]["meta"]["tool_name"], "session_search")
        self.assertIsNone(u0["meta"]["tool_name"])
        compacted = [k for k in RowTest.OWN if node[self.rid(k)]["meta"]["compacted"]]
        self.assertEqual(compacted, ["u1", "a1", "tool1", "a1b"])  # u0 lives on in its head copy
        self.assertEqual(u0["meta"]["ts"], self.ctx["turns"][0]["ts"])

    def test_each_node_names_its_turn_and_summary_rows_are_marked(self):
        # The graph lays the nodes out by turn: the turn of a node is the turn of its own Gantt bar.
        own_turn = {r["id"]: r["bars"][0]["t0"] for r in self.ctx["rows"] if r["session"] == "s-today"}
        self.assertEqual({n["id"]: n["meta"]["turn"] for n in self.s["nodes"]}, own_turn)
        summaries = [n["id"] for n in self.s["nodes"] if n["meta"]["summary"]]
        compress = [e["id"] for e in self.ctx["events"] if e["ev"] == "compress"]
        self.assertEqual(["compress-%s" % sid for sid in summaries], compress)
        self.assertTrue(all(isinstance(n["meta"]["summary"], bool) for n in self.s["nodes"]))

    def test_edges_are_nbr_and_call_only(self):
        ids = [n["id"] for n in self.s["nodes"]]
        nbr = [(e["source"], e["target"]) for e in self.s["edges"] if e["kind"] == "nbr"]
        self.assertEqual(nbr, list(zip(ids[1:], ids[:-1])))  # a message -> the one before it
        calls = {(e["source"], e["target"]) for e in self.s["edges"] if e["kind"] == "call"}
        self.assertEqual(calls, {(self.rid(t), self.rid(a)) for t, a in (
            ("tool1", "a1"), ("tool2ac", "a2c"), ("tool2bc", "a2c"), ("tool3a", "a3"), ("tool3b", "a3"))})
        self.assertEqual({e["kind"] for e in self.s["edges"]}, {"nbr", "call"})
        self.assertEqual({e["w"] for e in self.s["edges"]}, {1.0})

    def test_sessions_newest_first(self):
        got = logview.sessions(self.db)["sessions"]
        self.assertEqual([(s["id"], s["title"], s["blocks"]) for s in got],
                         [("s-empty", "", 0), ("s-today", "Today", 3), ("s-garden", "Balcony garden", 0),
                          ("s-bike", "Bike repair", 0)])
        self.assertEqual([s["started"] for s in got], sorted((s["started"] for s in got), reverse=True))
        self.assertEqual([s["messages"] for s in got], [0, len(self.s["nodes"]), 2, 4])
        self.assertEqual([s["id"] for s in logview.sessions(self.db, limit=1)["sessions"]], ["s-empty"])
        for bad in (0, -1, 501, "x", None):
            with self.assertRaises(ValueError, msg=bad):
                logview.sessions(self.db, limit=bad)

    def test_an_unknown_session_and_an_empty_one(self):
        for fn in (logview.context, logview.session):
            with self.assertRaises(KeyError):
                fn(self.db, "no-such-session")
        self.assertEqual(logview.context(self.db, "s-empty"), {"session": "s-empty", "events": [], "turns": [], "rows": []})
        self.assertEqual(logview.session(self.db, "s-empty"), {"session": "s-empty", "root": None, "nodes": [], "edges": []})
        with self.assertRaises(FileNotFoundError):
            logview.context(Path(self.tmp.name) / "none" / "state.db", "s-today")
        self.assertFalse((Path(self.tmp.name) / "none").exists())

    def test_hermes_tracked_connect_reads_the_same(self):
        connect = logview.hermes_connect()
        if connect is None:
            self.skipTest("hermes_cli is not importable")
        self.assertEqual(logview.context(self.db, "s-today", connect=connect), self.ctx)


class RouteTest(LogViewTest):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        top = Path(cls.tmp.name)
        for folder in ("arms/a", "arms/b/breadcrumbs", "arms/c/breadcrumbs"):
            (top / folder).mkdir(parents=True)
        shutil.copy2(cls.db, top / "arms" / "a" / "state.db")  # in the named folder itself
        shutil.copy2(cls.db, top / "arms" / "b" / "state.db")  # beside the named folder (its parent)
        cls.extra = top / "stores.local.json"
        cls.extra.write_text(json.dumps({"arm-a": str(top / "arms" / "a"), "arm-b": str(top / "arms" / "b" / "breadcrumbs"),
                                         "arm-c": str(top / "arms" / "c" / "breadcrumbs"), "demo": "/elsewhere"}))
        cls.before = files(cls.tmp.name)
        cls.api, cls.client = plugin_client(cls.home, cls.hermes, cls.extra)

    def get(self, route, **params):
        return self.client.get(PREFIX + route, params=params)

    def ok(self, route, **params):
        r = self.get(route, **params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_the_routes_give_the_views(self):
        for store in ("", "demo", "arm-a", "arm-b"):
            self.assertEqual(self.ok("/log/context", sid="s-today", store=store), self.ctx, store)
            self.assertEqual(self.ok("/log/session", sid="s-today", store=store), logview.session(self.db, "s-today"))
            self.assertEqual(self.ok("/log/sessions", store=store), logview.sessions(self.db))
        self.assertEqual(len(self.ok("/log/sessions", limit=2)["sessions"]), 2)

    def test_status_codes_and_error_bodies(self):
        cases = [("/log/context", {"sid": "no-such-session"}, 404), ("/log/session", {"sid": "no-such-session"}, 404),
                 ("/log/context", {}, 404), ("/log/session", {}, 404),
                 ("/log/sessions", {"limit": "x"}, 400), ("/log/sessions", {"limit": 0}, 400),
                 ("/log/context", {"sid": "s-today", "store": "default"}, 404),  # the root has no state.db
                 ("/log/context", {"sid": "s-today", "store": "nobody"}, 404),
                 ("/log/context", {"sid": "s-today", "store": "arm-c"}, 404)]  # no state.db in or beside it
        cases += [("/log/context", {"sid": "s-today", "store": bad}, 400) for bad in ("../demo", "demo/../../x",
                                                                                     ".hidden", "a b")]
        for route, params, status in cases:
            r = self.get(route, **params)
            self.assertEqual(r.status_code, status, (route, params, r.text))
            self.assertEqual(set(r.json()), {"error"}, (route, params))
        self.assertFalse((self.hermes / "state.db").exists())
        self.assertFalse((self.hermes / "profiles" / "nobody").exists())

    def test_the_crumb_routes_still_answer(self):
        r = self.get("/context", sid="s-today")  # the breadcrumbs route: this profile has no breadcrumbs store
        self.assertEqual((r.status_code, set(r.json())), (404, {"error"}))
        self.assertEqual(self.get("/log").status_code, 404)

    def test_the_hermes_loader_mounts_the_log_routes(self):
        try:
            from fastapi import FastAPI
            from hermes_cli import web_server_dashboard as dash
        except ImportError as e:
            self.skipTest("not the Python of a Hermes install: %s" % e)
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        plugin = dash._dashboard_plugin_entry(manifest, manifest["name"], ROOT / "dashboard", "user")
        app = FastAPI()
        with dash._plugin_api_mount_lock:
            self.assertTrue(dash._mount_one_plugin_api(app, plugin, {"breadcrumbs"}, set()))
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertLessEqual({PREFIX + p for p in ("/log/sessions", "/log/session", "/log/context")}, paths)

    def test_zz_nothing_was_written(self):  # zz: after the other route tests
        self.test_the_routes_give_the_views()
        self.test_status_codes_and_error_bodies()
        self.assertEqual(files(self.tmp.name), self.before)
        self.assertEqual(sha(self.db), self.digest)
        for wal in Path(self.tmp.name).rglob("state.db-wal"):  # SQLite's own file of a WAL reader: no data
            self.assertEqual(wal.stat().st_size, 0, wal)


class ReadOnlyTest(LogViewTest):
    def test_zz_no_read_changed_the_state_db(self):  # zz: after the other tests of this class
        logview.context(self.db, "s-today")
        logview.session(self.db, "s-today")
        logview.sessions(self.db)
        self.assertEqual(sha(self.db), self.digest)

    def test_a_reader_holds_no_lock(self):
        logview.context(self.db, "s-today")
        writer = sqlite3.connect(self.db, timeout=0)
        try:
            writer.execute("BEGIN IMMEDIATE")  # fails at once if a reader held a write-blocking lock
            writer.rollback()
        finally:
            writer.close()
        self.assertEqual(sha(self.db), self.digest)

    def test_logview_imports_only_the_standard_library(self):
        path = ROOT / "breadcrumbs" / "logview.py"
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for name in names:
                top = name.split(".")[0]
                self.assertTrue(top in sys.stdlib_module_names or name == "hermes_cli.sqlite_safe_read"
                                or top == "__future__", name)
                self.assertNotIn("logrefs", name)
                self.assertTrue(node.level == 0 if isinstance(node, ast.ImportFrom) else True, name)  # no package code


class FixtureTest(LogViewTest):
    def test_the_gantt_fixture_is_this_context(self):
        text = json.dumps(self.ctx, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
        if os.environ.get("SPACEGRAPH_WRITE_FIXTURES") == "1" or not FIXTURE.exists():
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(text, encoding="utf-8")
        self.assertEqual(FIXTURE.read_text(encoding="utf-8"), text,
                         "the log Gantt changed: check it, then rerun with SPACEGRAPH_WRITE_FIXTURES=1")
        for words in (INJECTED[0], "never mind that"):  # no injected text and no taken-back row in the fixture
            self.assertNotIn(words, text)


class TwoCompactionsTest(unittest.TestCase):
    """A head that the first compaction keeps leaves the context only at the compaction that archives
    its copy; a tail copy, at the compaction after the one that made it."""

    @unittest.skipIf(SessionDB is None, "needs the Python of a Hermes install (hermes_state)")
    def test_each_copy_ends_at_the_compaction_that_archives_it(self):
        with tempfile.TemporaryDirectory(prefix="sg-logview2-") as top:
            path = Path(top) / "state.db"
            assert SessionDB is not None
            db = SessionDB(db_path=path)
            ids, clock = {}, iter(T0 + 60 * n for n in range(1, 100))
            try:
                db.create_session("s1", "cli", model="test")
                for key, role, text in (("u0", "user", "first question"), ("a0", "assistant", "first answer"),
                                        ("u1", "user", "second question"), ("a1", "assistant", "second answer")):
                    ids[key] = db.append_message("s1", role, text, timestamp=next(clock))
                live = db.get_messages("s1")
                s1 = {"role": "user", "content": "summary one", "_compressed_summary": True, "timestamp": next(clock)}
                db.archive_and_compact("s1", live[:2] + [s1] + live[-1:], tail_count=1)  # keeps a head and a1
                ids["u2"] = db.append_message("s1", "user", "third question", timestamp=next(clock))
                ids["a2"] = db.append_message("s1", "assistant", "third answer", timestamp=next(clock))
                live = db.get_messages("s1")
                s2 = {"role": "user", "content": "summary two", "_compressed_summary": True, "timestamp": next(clock)}
                db.archive_and_compact("s1", [s2] + live[-1:], tail_count=1)  # no head this time
                ids["u3"] = db.append_message("s1", "user", "fourth question", timestamp=next(clock))
            finally:
                db.close()
            ctx = logview.context(path, "s1")
        stops = [i for i, e in enumerate(ctx["events"]) if e["ev"] == "compress"]
        self.assertEqual([ctx["events"][i]["turn"] for i in stops], [1, 2])
        ends = {r["label"]: (r["bars"][0]["to"], r["bars"][0]["t1"]) for r in ctx["rows"]}
        first, second = (stops[0], 1), (stops[1], 2)
        self.assertEqual(ends, {"user first question": second, "assistant first answer": second,  # the kept head
                                "user second question": first, "user summary one": second,
                                "assistant second answer": second,  # the tail copy of the first compaction
                                "user third question": second, "user summary two": (None, None),
                                "assistant third answer": (None, None), "user fourth question": (None, None)})
        self.assertEqual([t["crumb"] for t in ctx["turns"]][:2], [str(ids["u0"]), str(ids["u1"])])
        self.assertEqual(len(ctx["turns"]), 4)


if __name__ == "__main__":
    unittest.main()
