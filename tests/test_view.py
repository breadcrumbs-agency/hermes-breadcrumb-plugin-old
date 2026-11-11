import calendar
import contextlib
import importlib.util
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import view
from breadcrumbs.hermes import BreadcrumbsProvider

SESSIONS = {
    "tea1": [("how long should green tea steep in the kettle water", "steep green tea two minutes; kettle water near 80 C")],
    "tea2": [("the kettle boils too fast for black tea", "black tea likes the kettle water just off the boil")],
    "bike": [("my bike chain skips on the small cog", "replace the worn chain and check every cog for wear")],
}


def ids(rows):
    return [r["id"] for r in rows]


class TrailTest(unittest.TestCase):
    """The trail is SpaceGraph's zoom stack: push a new crumb, keep 8, show the rest as one gap."""

    def test_a_new_crumb_is_pushed_and_the_oldest_leaves_after_eight(self):
        t = view.Trail()
        for i in range(10):
            t.visit("c%07d" % i, 0.1)
        self.assertEqual(ids(t.stack), ["c%07d" % i for i in range(2, 10)])
        self.assertEqual(t.dropped, 2)

    def test_the_top_crumb_again_changes_only_its_level(self):
        t = view.Trail()
        t.visit("a0000000", 0.1)
        t.visit("b0000000", 0.1)
        t.visit("b0000000", 0.8)
        self.assertEqual([(e["id"], e["level"]) for e in t.stack], [("a0000000", 0.1), ("b0000000", 0.8)])

    def test_a_crumb_lower_in_the_stack_goes_back_to_it(self):
        t = view.Trail()
        for c in ("a0000000", "b0000000", "c0000000"):
            t.visit(c, 0.2)
        t.visit("a0000000", 0.5)
        self.assertEqual([(e["id"], e["level"]) for e in t.stack], [("a0000000", 0.5)])

    def test_the_status_line_runs_oldest_to_newest_with_a_gap_mark(self):
        t = view.Trail()
        for i in range(9):
            t.visit("c%07d" % i, 0.1, "turn: n%d" % i)
        line = t.line()
        self.assertTrue(line.startswith("\u2026 \u203a "))
        self.assertTrue(line.endswith("turn: n8"))
        self.assertEqual(view.Trail.from_dict(json.loads(json.dumps(t.to_dict()))).line(), line)


class ViewData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after every View closes (Windows)
        self.p = BreadcrumbsProvider()
        self.p.initialize("tea1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        for sid, turns in SESSIONS.items():
            for u, a in turns:
                self.p.sync_turn(u, a, session_id=sid)
        self.sess = {sid: self.p.session_crumb(sid) for sid in SESSIONS}
        self.root = os.path.join(self.tmp.name, "breadcrumbs")

    def tearDown(self):
        self.p.shutdown()

    def open(self):
        """A View that the test closes (Windows cannot remove a folder with open files)."""
        v = view.open_view(self.root)
        self.addCleanup(v.close)
        return v


class ReadOnlyViewTest(ViewData):
    def test_a_folder_without_a_store_raises_and_gets_no_file(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        for path in (os.path.join(self.tmp.name, "nope"), empty):
            with self.assertRaises(FileNotFoundError):
                view.View(path)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "nope")))
        self.assertEqual(os.listdir(empty), [])

    def test_a_view_changes_no_file_and_close_releases_it(self):
        self.p.shutdown()
        before = snapshot(self.root)
        v = view.View(self.root)
        v.graph()
        v.find("kettle")
        v.crumb(self.sess["tea1"], 0.5)
        v.close()
        v.close()  # a second close does nothing
        self.assertTrue(v.store._reader.closed)
        self.assertEqual(snapshot(self.root), before)


class GraphTest(ViewData):
    def test_one_node_per_session_with_cue_words_and_a_size(self):
        g = self.open().graph()
        nodes = {n["id"]: n for n in g["nodes"]}
        self.assertEqual(set(nodes), set(self.sess.values()))
        tea = nodes[self.sess["tea1"]]
        self.assertEqual((tea["kind"], tea["session"]), ("sess", "tea1"))
        self.assertIn("tea", tea["cues"])
        self.assertGreater(tea["tokens"], 0)
        self.assertTrue(all(0 < n["pri"] <= 1 for n in g["nodes"]))

    def test_sessions_that_share_cue_words_are_joined(self):
        g = self.open().graph()
        pairs = {frozenset((e["source"], e["target"])) for e in g["edges"] if e["kind"] == "cue"}
        self.assertIn(frozenset((self.sess["tea1"], self.sess["tea2"])), pairs)
        self.assertFalse(any(self.sess["bike"] in p for p in pairs))

    def test_a_root_opens_one_level_of_parts(self):
        g = self.open().graph(root=self.sess["bike"])
        kids = [e["target"] for e in g["edges"] if e["kind"] == "part" and e["source"] == self.sess["bike"]]
        self.assertEqual(len(kids), 2)
        self.assertTrue({"turn"} <= {n["kind"] for n in g["nodes"]})

    def test_find_groups_hits_by_session(self):
        hits = self.open().find("bike chain cog")
        self.assertEqual(hits[0]["session"], self.sess["bike"])
        self.assertTrue(hits[0]["hits"])

    def test_the_last_moved_sessions_come_first_and_the_limit_keeps_them(self):
        refs = os.path.join(self.root, "refs")
        for i, sid in enumerate(("bike", "tea1", "tea2")):  # tea2 moved last
            os.utime(os.path.join(refs, "sess-" + sid), (1000 + i, 1000 + i))
        g = self.open().graph(limit=2)
        self.assertEqual(ids(g["nodes"]), [self.sess["tea2"], self.sess["tea1"]])


class PathTest(ViewData):
    """The page follows the agent into a crumb: it needs the crumbs from the session down to it."""

    def test_a_serving_view_does_not_block_the_provider(self):
        v = self.open()
        v.graph()
        v.graph(root=self.sess["bike"])
        v.find("kettle")
        v.crumb(self.sess["tea1"], 0.5)
        v.path(self.sess["tea2"])
        self.p.index.db.execute("PRAGMA busy_timeout = 300")
        out = json.loads(self.p.handle_tool_call("mcp__crumb", {"id": self.sess["tea2"], "level": 0.5}))
        self.assertIn("text", out)
        self.p.sync_turn("one more question about tea", "one more answer", session_id="tea2")

    def test_a_turn_path_starts_at_its_session(self):
        v = self.open()
        sess = self.sess["bike"]
        turn = v.graph(root=sess)["nodes"][2]["id"]
        self.assertEqual(v.path(turn), [sess, turn])

    def test_a_session_path_is_the_session(self):
        sess = self.sess["tea2"]
        self.assertEqual(self.open().path(sess), [sess])

    def test_a_text_range_path_goes_through_its_turn_from_a_given_start(self):
        v = self.open()
        sess = self.sess["bike"]
        turn = v.graph(root=sess)["nodes"][1]["id"]
        text = v.graph(root=turn)["nodes"][1]["id"]
        self.assertIn(":", text)
        self.assertEqual(v.path(text), [])  # a text does not know its session
        self.assertEqual(v.path(text, within=sess), [sess, turn, text])


class ProviderTrailTest(ViewData):
    def test_crumb_calls_write_the_trail_file(self):
        self.p.handle_tool_call("mcp__crumb", {"id": self.sess["tea2"], "level": 0.2})
        self.p.handle_tool_call("mcp__crumb", {"id": self.sess["bike"], "level": 0.5})
        with open(os.path.join(self.root, "trail.json")) as f:
            t = json.load(f)
        self.assertEqual(ids(t["stack"]), [self.sess["tea2"], self.sess["bike"]])
        self.assertTrue(t["stack"][-1]["label"].startswith("sess"))
        self.assertEqual(self.open().trail()["stack"], t["stack"])


class ServerTest(ViewData):
    def setUp(self):
        super().setUp()
        self.server = view.make_server(self.root, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.token = view.token(self.root)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, token=True):
        sep = "&" if "?" in path else "?"
        url = self.base + path + (sep + "k=" + self.token if token else "")
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def test_the_api_needs_the_token(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/graph", token=False)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        status, kind, body = self.get("/api/graph")
        self.assertEqual((status, kind.split(";")[0]), (200, "application/json"))
        self.assertEqual(len(json.loads(body)["nodes"]), 3)

    def test_the_page_is_served_without_the_token(self):
        status, kind, body = self.get("/", token=False)
        self.assertEqual((status, kind.split(";")[0]), (200, "text/html"))
        self.assertIn(b"<canvas", body)

    def test_crumb_renders_at_a_level(self):
        sess = self.sess["bike"]
        _, _, body = self.get("/api/crumb?id=%s&level=1" % sess)
        self.assertIn("my bike chain skips", json.loads(body)["text"])
        _, _, body = self.get("/api/crumb?id=%s&level=0" % sess)
        self.assertEqual(len(json.loads(body)["text"].splitlines()), 1)

    def test_path_and_an_unknown_id(self):
        sess = self.sess["tea1"]
        _, _, body = self.get("/api/path?id=" + sess)
        self.assertEqual(json.loads(body), [sess])
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/api/crumb?id=0badf00d&level=1")
        self.assertEqual(e.exception.code, 404)
        e.exception.close()

    def test_reads_change_no_file(self):
        self.p.shutdown()
        before = snapshot(self.root)
        for path in ("/api/graph", "/api/graph?root=" + self.sess["tea1"], "/api/find?q=kettle",
                     "/api/crumb?id=%s&level=0.5" % self.sess["tea1"], "/api/trail"):
            self.get(path)
        self.assertEqual(snapshot(self.root), before)


def snapshot(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            st = os.stat(os.path.join(base, name))
            out[os.path.join(base, name)] = (st.st_size, st.st_mtime_ns)
    return out



class BcCommandTest(ViewData):
    """`bc` is the dispatch handbook's way for a helper without the crumb tool (a delegate_task child): it
    runs beside a live gateway, so it reads like the View and shows what the crumb tool shows."""

    def bc(self, *argv):
        import contextlib
        import io
        from breadcrumbs import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", self.root, "bc", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_bc_shows_what_the_crumb_tool_shows_and_changes_no_file(self):
        tool = json.loads(self.p.handle_tool_call("crumb", {"id": self.sess["tea1"], "level": 1}))["text"]
        self.p.shutdown()
        before = snapshot(self.root)
        code, out, _ = self.bc(self.sess["tea1"], "1")
        self.assertEqual(code, 0)
        self.assertEqual(out, tool)
        self.assertIn("steep green tea two minutes", out)
        self.assertEqual(snapshot(self.root), before)

    def test_bc_answers_while_the_gateway_still_holds_the_store(self):
        code, out, _ = self.bc(self.sess["bike"], "1")
        self.assertEqual(code, 0)
        self.assertIn("replace the worn chain", out)

    def test_bc_on_a_folder_without_a_store_says_so_and_makes_no_file(self):
        self.root = os.path.join(self.tmp.name, "nope")
        code, _, err = self.bc("deadbeef", "1")
        self.assertEqual(code, 1)
        self.assertIn("no store", err)
        self.assertFalse(os.path.exists(self.root))

    def test_bc_names_an_id_that_the_store_does_not_have(self):
        code, _, err = self.bc("deadbeef", "1")
        self.assertEqual(code, 1)
        self.assertIn("deadbeef", err)


def ace_line(sess, minute, act, raw, sent, last=0, base=500, layer=0, lay=0, target=1000, folds=0):
    """One whole line of ace.log, as Ace._log writes it."""
    return ("2026-01-02T03:%02d:00 pid=7 sess=%s act=%s in=%d out=%d last=%d base=%s tpb=0.3000 r=0.3000 folds=%d "
            "layer=%d target=%d gain=1.0000 lay=%d\n") % (minute, sess, act, raw, sent, last, base, folds, layer, target, lay)


class AceTrackTest(ViewData):
    """The size of the context from ace.log: the points, the candles, the pie and the totals of every session."""
    S1, S2 = "local-0123456789abcdef01", "local-fedcba9876543210ff"

    def log(self, *lines, name="ace.log"):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write("".join(lines))

    def sawtooth(self):
        # grows to 1,000 (the target), a fold cuts it to 900 (10%), it grows again, a second fold cuts 850 of 950
        return [ace_line(self.S1, 0, "pass", 400, 400), ace_line(self.S1, 1, "pass", 700, 700, last=420),
                ace_line(self.S1, 2, "pass", 990, 990, last=710),
                ace_line(self.S1, 3, "fold", 1200, 700, last=1000, layer=9, lay=120, folds=1),
                ace_line(self.S1, 4, "keep", 1300, 800, last=690, layer=9, lay=120, folds=1),
                ace_line(self.S1, 5, "keep", 1500, 950, last=810, layer=9, lay=120, folds=1),
                ace_line(self.S1, 6, "fold", 1600, 855, last=960, layer=11, lay=140, folds=2)]

    def test_each_request_is_a_point_and_a_fold_is_a_drop_of_the_sent_size(self):
        self.log(*self.sawtooth())
        d = self.open().ace()
        pts = d["points"]
        self.assertEqual([p["act"] for p in pts], ["pass", "pass", "pass", "fold", "keep", "keep", "fold"])
        self.assertEqual([p["sent"] for p in pts], [400, 700, 990, 700, 800, 950, 855])
        self.assertEqual(pts[3]["drop"], round(1 - 700 / 990, 4))  # the first fold: 29% of the request before
        self.assertEqual(pts[6]["drop"], 0.1)                      # a gentle fold: a tenth
        self.assertEqual(pts[4]["drop"], 0.0)                      # a request that grew cuts nothing
        self.assertEqual(d["session"], d["current"])
        self.assertEqual(d["target"], 1000)

    def test_the_models_count_of_a_request_is_read_from_the_request_after_it(self):
        self.log(*self.sawtooth())
        pts = self.open().ace()["points"]
        self.assertEqual([p["seen"] for p in pts], [420, 710, 1000, 690, 810, 960, None])

    def test_a_session_matches_by_the_prefix_that_a_log_line_holds(self):
        self.log(*self.sawtooth(), ace_line(self.S2, 9, "pass", 100, 100))
        v = self.open()
        self.assertEqual(v.ace(self.S1 + "abcdef0123456789")["session"], self.S1)  # the full session id of Hermes
        self.assertEqual(v.ace()["session"], self.S2)                              # the newest line
        none = v.ace("local-00000000000000000000ffff")
        self.assertEqual((none["session"], none["points"], none["mix"]), ("", [], None))
        self.assertEqual(none["all"]["sessions"], 2)

    def test_candles_are_open_high_low_close_of_the_sent_size(self):
        self.log(*self.sawtooth())
        c = self.open().ace(per=3)["candles"]
        self.assertEqual([(x["open"], x["high"], x["low"], x["close"], x["folds"], x["n"]) for x in c],
                         [(400, 990, 400, 990, 0, 3), (700, 950, 700, 950, 1, 3), (855, 855, 855, 855, 1, 1)])
        self.assertEqual(len(self.open().ace()["candles"]), 7)  # few requests: one candle each

    def test_the_pie_splits_the_last_request_into_base_layer_exact_and_held(self):
        self.log(*self.sawtooth())
        mix = self.open().ace()["mix"]
        self.assertEqual((mix["base"], mix["layer"], mix["exact"], mix["sent"], mix["raw"], mix["held"]),
                         (500, 140, 855 - 500 - 140, 855, 1600, 1600 - 855))
        self.assertEqual(mix["unknown"], [])

    def test_a_line_from_before_the_layer_size_was_logged_names_what_it_does_not_know(self):
        old = ("2026-01-02T03:00:00 pid=7 sess=%s act=keep in=900 out=600 last=0 base=100 tpb=0.3000 r=0.3000 "
               "folds=1 layer=4\n") % self.S1
        self.log(old)
        mix = self.open().ace()["mix"]
        self.assertEqual((mix["layer"], mix["exact"], mix["unknown"]), (0, 500, ["lay"]))

    def test_the_totals_bin_every_fold_by_the_share_it_cut_and_skip_a_first_request(self):
        self.log(*self.sawtooth(), ace_line(self.S2, 10, "fold", 5000, 100))  # a fold that opens a session cuts nothing
        a = self.open().ace()["all"]
        self.assertEqual((a["requests"], a["sessions"], a["folds"]), (8, 2, 2))
        self.assertEqual(a["bins"], [0, 0, 1, 1, 0])  # the cuts of 10% (10 to 20%) and 29% (20 to 40%)
        self.assertEqual(a["acts"], {"pass": 3, "fold": 3, "keep": 2})
        self.assertEqual([r["sess"] for r in a["rows"]], [self.S2, self.S1])  # the newest first
        self.assertEqual(a["rows"][1]["folds"], 2)

    def test_the_older_log_file_comes_first_and_a_damaged_or_unfinished_line_is_skipped(self):
        self.log(ace_line(self.S1, 0, "pass", 100, 100), name="ace.log.1")
        self.log(ace_line(self.S1, 1, "pass", 200, 200), "garbage\n", "2026-01-02T03:02:00 pid=7 sess=%s act=pass in=x out=1\n" % self.S1,
                 ace_line(self.S1, 3, "pass", 300, 300)[:-1])  # no line end: not a whole line
        pts = self.open().ace()["points"]
        self.assertEqual([p["sent"] for p in pts], [100, 200])

    def test_no_log_gives_an_empty_answer_and_a_view_writes_nothing(self):
        before = sorted(os.listdir(self.root))
        d = self.open().ace()
        self.assertEqual((d["session"], d["points"], d["candles"], d["mix"]), ("", [], [], None))
        self.assertEqual(d["all"]["requests"], 0)
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_the_route_answers_over_http_with_the_token(self):
        self.log(*self.sawtooth())
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/api/ace?per=2&k=%s" % (server.server_address[1], view.token(self.root))
        with urllib.request.urlopen(url) as r:
            d = json.load(r)
        self.assertEqual((len(d["points"]), len(d["candles"])), (7, 4))


SECRET = "SECRET-TEXT-X"  # planted in every text column of the fixture state.db that measures() must not read
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONNET, OPUS, HAIKU = "claude-sonnet-5-5", "claude-opus-5", "claude-haiku-5"
REPO_A, REPO_B = "/Users/demo/code/home-notes", "/Users/demo/code/bike-fix"  # made-up codebases


def stamp(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def ace_log_line(ts, sess, act, raw, sent, last=0, base=500, layer=0, lay=0, target=1000, folds=0):
    """One whole line of ace.log as Ace._log writes it, at a given time."""
    return ("%s pid=7 sess=%s act=%s in=%d out=%d last=%d base=%s tpb=0.3000 r=0.3000 folds=%d layer=%d target=%d "
            "gain=1.0000 lay=%d\n") % (stamp(ts), sess, act, raw, sent, last, base, folds, layer, target, lay)


def usage_log_line(ts, sess, model, prompt, task="main", cr=0, cw=0, out=0, rsn=0, fin="stop", turn="-", req="-", n=1):
    """One whole line of usage.log in the fixed format of breadcrumbs/usage_log.py (numbers, ids and enums only)."""
    return ("%s sess=%s turn=%s req=%s n=%d task=%s model=%s resp=%s prov=anthropic in=%d cr=%d cw=%d out=%d rsn=%d "
            "prompt=%d est=%d msgs=12 tools=30 dur=1.500 fin=%s\n") % (stamp(ts), sess, turn, req, n, task, model, model,
                                                                      prompt - cr - cw, cr, cw, out, rsn, prompt, prompt, fin)


def seed_module():
    spec = importlib.util.spec_from_file_location("demo_seed_measures", os.path.join(ROOT, "demo", "seed.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state_db(path, sessions, usage, secret=SECRET):
    """A state.db with the columns that measures() reads and the ones it must not: every text column that could hold
    text, and the messages table, holds the planted secret."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, created_source TEXT, "
               "started_at REAL, ended_at REAL, end_reason TEXT, last_activity_at REAL, message_count INTEGER, "
               "tool_call_count INTEGER, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
               "cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, "
               "actual_cost_usd REAL, cwd TEXT, git_repo_root TEXT, git_branch TEXT, title TEXT, display_name TEXT, "
               "system_prompt TEXT, last_activity_description TEXT, origin_json TEXT, handoff_state TEXT, "
               "handoff_platform TEXT, handoff_error TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT, "
               "api_content TEXT, timestamp REAL)")
    db.execute("CREATE TABLE session_model_usage (session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT, "
               "billing_mode TEXT, task TEXT, api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
               "cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, estimated_cost_usd REAL, "
               "actual_cost_usd REAL, cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL, "
               "PRIMARY KEY (session_id, model, task))")
    for s in sessions:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (s["id"], s.get("parent"), "cli", s.get("how", "cli"), s["t0"], s.get("t1"), "cli_close", s.get("last"),
                    s.get("messages", 10), s.get("tools", 4), s.get("api", 6), s.get("in", 1000), s.get("out", 200),
                    s.get("cr", 5000), s.get("cw", 300), s.get("rsn", 50), 9.99, 0.0, s.get("cwd"), s.get("repo"),
                    s.get("branch"), secret, secret, secret, secret, secret, secret, secret, secret))
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, api_content, timestamp) "
                   "VALUES (?,?,?,?,?,?)", (s["id"], "user", secret, secret, secret, s["t0"]))
    for u in usage:
        db.execute("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (u["sid"], u["model"], "anthropic", secret, "", u.get("task", ""), 3, 100, 20, 500, 30, 5,
                    u.get("est", 0.0), u.get("actual", 0.0), u.get("status"), u.get("source"), u["t0"], u.get("t1", u["t0"])))
    db.commit()
    db.close()


def build_fixture(home, far_days=0, secret=SECRET, spread=100):
    """The demo store (demo/seed.py) at <home>/breadcrumbs and, beside it, what a real profile has: state.db (made-up
    numbers; one chat 'today', its continuation, helpers it delegated, eight past chats in two codebases, one chat that
    the store lacks), ace.log and usage.log. far_days moves the chat the store lacks that many days into the past. spread is
    the seconds between two requests of the chat 'today' (100: they share an hour column; a picture wants 900); the times of
    its helpers and of its cost rows move with it, and at 100 every number is the one that the tests count.
    Returns {"root", "t": {sid: (start, end)}, "ace": {sid: [lines]}, "usage": {sid: [lines]}}."""
    with contextlib.redirect_stdout(io.StringIO()):
        seed_module().main(home)
    root = os.path.join(home, "breadcrumbs")
    v = view.View(root)
    try:
        span = {}
        for name in v.refs.names():
            if name.startswith("sess-"):
                ref = v.refs.get(name)
                stamps = [x for x in v.walk(ref).ts if x]
                span[v.crumb_or_none(ref).meta["sess"]] = (min(stamps), max(stamps))
    finally:
        v.close()
    H = 3600
    t0, t1 = span["today"]
    sc = spread / 100

    def o(v):  # an offset from the start of 'today', scaled by spread
        return int(v * sc)

    sessions = []
    for i, sid in enumerate(("tea", "tea-2", "garden", "bread", "bike", "bike-2", "parser", "trip")):
        a, b = span[sid]
        sessions.append({"id": sid, "t0": a, "t1": b, "last": b, "in": 800 + 100 * i, "out": 150 + 10 * i, "cr": 4000 + 50 * i,
                         "repo": REPO_A if i < 4 else REPO_B if sid != "trip" else None,
                         "cwd": "/Users/demo/code/home-notes/trip" if sid == "trip" else None,
                         "branch": "main" if i < 4 else "fix/cassette" if sid != "trip" else None})
    far = t0 - 20 * H - far_days * 86400
    sessions += [
        {"id": "today", "t0": t0, "t1": t1, "last": t1, "in": 52300, "out": 6100, "cr": 410000, "cw": 22000, "rsn": 3200,
         "api": 14, "tools": 9, "messages": 21, "repo": REPO_A, "branch": "main"},
        {"id": "today-h1", "parent": "today", "how": "subagent", "t0": t0 + o(600), "t1": t0 + o(1000), "in": 7000, "out": 900,
         "cr": 30000, "repo": REPO_A, "branch": "feature/kettle"},
        {"id": "today-h2", "parent": "today", "how": "subagent", "t0": t0 + o(900), "t1": t0 + o(1300), "in": 9000, "out": 1200,
         "cr": 41000, "repo": REPO_A, "branch": "feature/kettle"},
        {"id": "today-cont", "parent": "today", "how": "cli", "t0": t1 + 6 * H, "t1": t1 + 8 * H, "in": 33000, "out": 4000,
         "cr": 250000, "cw": 9000, "api": 9, "repo": REPO_A, "branch": "main"},
        {"id": "today-cont-h", "parent": "today-cont", "how": "subagent", "t0": t1 + 7 * H, "t1": t1 + 7 * H + 600,
         "in": 4000, "out": 500, "cr": 12000, "repo": REPO_A, "branch": "main"},
        {"id": "ghost", "t0": far, "t1": far + 1800, "in": 2500, "out": 300, "cr": 9000, "repo": REPO_B, "branch": "fix/cassette"}]
    usage = [
        {"sid": "today", "model": SONNET, "t0": t0, "t1": t1, "est": 1.2345, "status": "estimated", "source": "provider_models_api"},
        {"sid": "today", "model": OPUS, "t0": t0 + o(700), "t1": t1, "est": 3.1, "status": "estimated", "source": "provider_models_api"},
        {"sid": "today", "model": HAIKU, "task": "title_generation", "t0": t0 + 2, "est": 0.0021, "status": "estimated",
         "source": "official_docs_snapshot"},
        {"sid": "today", "model": HAIKU, "task": "compression", "t0": t0 + o(1000), "est": 0.01, "status": "estimated",
         "source": "official_docs_snapshot"},
        {"sid": "today-h1", "model": SONNET, "t0": t0 + o(600), "t1": t0 + o(1000), "est": 0.4, "status": "estimated",
         "source": "provider_models_api"},
        {"sid": "today-h2", "model": SONNET, "t0": t0 + o(900), "t1": t0 + o(1300), "est": 0.55, "status": "estimated",
         "source": "provider_models_api"},
        {"sid": "today-cont", "model": OPUS, "t0": t1 + 6 * H, "t1": t1 + 8 * H, "est": 2.5, "status": "estimated",
         "source": "provider_models_api"},
        {"sid": "today-cont-h", "model": SONNET, "t0": t1 + 7 * H, "est": 0.2, "actual": 0.25, "status": "estimated",
         "source": "provider_models_api"},
        {"sid": "tea", "model": SONNET, "t0": span["tea"][0], "est": 0.0, "status": "included", "source": "none"},
        {"sid": "bike", "model": SONNET, "t0": span["bike"][0], "est": 0.0, "status": "unknown", "source": "none"},
        {"sid": "bike-2", "model": SONNET, "t0": span["bike-2"][0], "est": 0.0},
        {"sid": "parser", "model": SONNET, "t0": span["parser"][0], "est": 0.12, "status": "estimated", "source": "provider_models_api"},
        {"sid": "ghost", "model": OPUS, "t0": far, "t1": far + 1800, "est": 1.5, "status": "estimated", "source": "provider_models_api"}]
    state_db(os.path.join(home, "state.db"), sessions, usage, secret)
    # ace.log: eight requests of 'today' (two folds) and six of its continuation; usage.log: the provider's count of each,
    # a chat that went from Sonnet to Opus, the calls of two auxiliary tasks, and two helpers
    ace, use = {"today": [], "today-cont": []}, {"today": [], "today-cont": [], "today-h1": [], "today-h2": [], "today-cont-h": []}
    plan = [("pass", 400, 400), ("pass", 700, 700), ("pass", 990, 990), ("fold", 1200, 700), ("keep", 1300, 800),
            ("keep", 1500, 950), ("keep", 1700, 1010), ("fold", 1900, 880)]
    for sid, start, model_of in (("today", t0 + 5, lambda k: SONNET if k < 5 else OPUS),
                                 ("today-cont", t1 + 6 * H + 5, lambda k: OPUS)):
        step = spread if sid == "today" else 3 * spread + 300  # 600 at the default spread
        for k, (act, raw, sent) in enumerate(plan[:8 if sid == "today" else 6]):
            ts = start + k * step
            ace[sid].append(ace_log_line(ts, sid, act, raw, sent, base=500, layer=9 if k > 2 else 0, lay=120 if k > 2 else 0,
                                         folds=sum(1 for p in plan[:k + 1] if p[0] == "fold")))
            use[sid].append(usage_log_line(ts + 4, sid, model_of(k), sent + 60, cr=sent // 2, cw=40 if act == "fold" else 0,
                                           out=120 + k, rsn=30 if k == 3 else 0, turn="t%d" % (k // 2 + 1), req="r%d" % k))
    use["today"].insert(0, usage_log_line(t0 + 2, "today", HAIKU, 300, task="title_generation", out=12))
    use["today"].insert(4, usage_log_line(t0 + 3 * spread + 12, "today", HAIKU, 2400, task="compression", out=400))
    for sid, base, n, model in (("today-h1", t0 + o(610), 3, SONNET), ("today-h2", t0 + o(910), 2, SONNET),
                                ("today-cont-h", t1 + 7 * H + 20, 2, SONNET)):
        for k in range(n):
            use[sid].append(usage_log_line(base + k * o(60), sid, model, 1500 + 700 * k, cr=900, out=90))
    with open(os.path.join(root, "ace.log"), "w", encoding="utf-8") as f:
        f.write("".join(l for lines in ace.values() for l in lines))
    with open(os.path.join(root, "usage.log"), "w", encoding="utf-8") as f:
        f.write("".join(l for lines in use.values() for l in lines))
    return {"root": root, "t": span, "ace": ace, "usage": use, "far": far}


class MeasuresTest(unittest.TestCase):
    """View.measures: numbers with four keys (scope, context, family, inclusion), each with its source. The fixture is the
    seeded demo store with a made-up state.db, ace.log and usage.log beside it (build_fixture)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.home = cls.tmp.name
        cls.fx = build_fixture(cls.home)
        cls.root = cls.fx["root"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.v = view.View(self.root)
        self.addCleanup(self.v.close)

    def load(self, v=None, **kw):
        """The whole family: the top level, then the members of every row that has some. -> (top answer, {key: row}, facts)
        Every measure, the ones that walk the crumb trees too (m="all"), unless the test names some."""
        v = v or self.v
        kw.setdefault("m", "all")
        top = v.measures(**kw)
        rows, facts = {r["key"]: r for r in top["dims"]["family"]}, list(top["facts"])
        todo = [k for k, r in rows.items() if r["more"]]
        while todo:
            batch, todo = todo[:7], todo[7:]  # several keys in one call: root="a|b"
            ans = v.measures(root="|".join(batch), **kw)
            for r in ans["dims"]["family"]:
                rows[r["key"]] = r
                if r["more"]:
                    todo.append(r["key"])
            facts += ans["facts"]
        return top, rows, facts

    UNITS = ({}, {"unit": "turn", "sid": "today"}, {"unit": "event", "sid": "today"}, {"unit": "request", "sid": "today"})

    def test_inclusive_is_self_plus_all_descendants_exactly(self):
        checked = {}
        for kw in self.UNITS:
            top, rows, facts = self.load(**kw)
            by = {(f[0], f[1], f[3], f[7], f[8]): (f[4], f[5]) for f in facts}
            members = {}
            for r in rows.values():
                if r["parent"]:
                    members.setdefault(r["parent"], []).append(r["key"])
            for (row, ctx, m, model, task), (own, incl) in by.items():
                kids = [by.get((k, ctx, m, model, task), (None, 0))[1] for k in members.get(row, ())]
                want = max([own or 0] + kids) if view.MEASURE[m]["agg"] == "max" else (own or 0) + sum(kids)
                self.assertAlmostEqual(incl, want, places=6, msg=(kw, row, ctx, m))
                checked[m] = checked.get(m, 0) + 1
        self.assertEqual(set(checked), set(view.MEASURE), "every measure is checked in some unit")

    def test_each_column_total_is_the_sum_of_its_cells_and_of_the_roots(self):
        for kw in self.UNITS:
            top, rows, facts = self.load(**kw)
            cells, roots = {}, {}
            for f in facts:
                key = (f[3], f[1], f[7], f[8])
                if f[4] is not None:
                    cells[key] = max(cells.get(key, 0), f[4]) if view.MEASURE[f[3]]["agg"] == "max" else cells.get(key, 0) + f[4]
                if rows[f[0]]["parent"] is None:
                    roots[key] = max(roots.get(key, 0), f[5]) if view.MEASURE[f[3]]["agg"] == "max" else roots.get(key, 0) + f[5]
            totals = {(m, c, model, task): v for m, c, v, model, task in top["totals"]}
            self.assertEqual(set(totals), set(cells), kw)
            for key, v in totals.items():
                self.assertAlmostEqual(v, cells[key], places=6, msg=(kw, key))  # the sum of all self numbers
                self.assertAlmostEqual(v, roots[key], places=6, msg=(kw, key))  # the incl of the roots

    def test_the_planted_text_is_in_no_output(self):
        outs = []
        for kw in self.UNITS:
            top, rows, facts = self.load(lines=True, incl=True, **kw)
            outs += [top, rows, facts]
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        k = view.token(self.root)
        for q in ("", "&unit=turn&sid=today", "&unit=request&sid=today&lines=1&incl=1", "&unit=event&sid=today", "&m=all&names=1"):
            with urllib.request.urlopen("http://127.0.0.1:%d/api/measures?k=%s%s" % (server.server_address[1], k, q)) as r:
                outs.append(r.read().decode("utf-8"))
        text = json.dumps(outs, ensure_ascii=False)
        self.assertGreater(len(text), 50000)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("secret", text.lower())

    def test_only_the_named_columns_of_two_tables_are_read(self):
        reads = set()

        def connect(*a, **k):
            conn = sqlite3.connect(*a, **k)
            conn.set_authorizer(lambda action, t, c, db, src: (reads.add((t, c)) if action == sqlite3.SQLITE_READ else None)
                                or sqlite3.SQLITE_OK)
            return conn

        self.v.db_connect = connect
        for kw in self.UNITS:
            self.load(**kw)
        tables = {t for t, c in reads if not t.startswith("sqlite_")}
        self.assertEqual(tables, {"sessions", "session_model_usage"}, "never the messages table")
        cols = {(t, c) for t, c in reads if not t.startswith("sqlite_")}
        self.assertTrue(cols <= {("sessions", c) for c in view.SESSION_COLS} | {("session_model_usage", c) for c in view.MODEL_USAGE_COLS})
        banned = {"title", "display_name", "system_prompt", "last_activity_description", "origin_json", "handoff_state",
                  "handoff_platform", "handoff_error", "content", "tool_calls", "api_content", "billing_base_url"}
        self.assertFalse({c for t, c in cols} & banned)
        self.assertIn(("sessions", "git_repo_root"), cols)

    def test_state_db_is_not_changed_and_gets_no_journal(self):
        db = os.path.join(self.home, "state.db")
        before = (os.stat(db).st_size, os.stat(db).st_mtime_ns, sorted(os.listdir(self.home)))
        for kw in self.UNITS:
            self.load(lines=True, **kw)
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:%d/api/measures?k=%s" % (server.server_address[1], view.token(self.root))
        urllib.request.urlopen(url).read()
        after = (os.stat(db).st_size, os.stat(db).st_mtime_ns, sorted(os.listdir(self.home)))
        self.assertEqual(after, before)
        self.assertFalse([n for n in os.listdir(self.home) if n.startswith("state.db-")])

    def test_the_route_needs_the_token_and_answers_errors_in_the_api_way(self):
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d/api/measures" % server.server_address[1]
        k = "k=" + view.token(self.root)

        def code(url):
            try:
                with urllib.request.urlopen(url) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                e.close()
                return e.code, None

        self.assertEqual(code(base)[0], 403)
        self.assertEqual(code(base + "?unit=turn&sid=today")[0], 403)
        status, d = code(base + "?" + k)
        self.assertEqual((status, d["unit"], d["has_db"]), (200, "time", True))
        self.assertEqual(code(base + "?unit=week&" + k)[0], 400)
        self.assertEqual(code(base + "?unit=turn&" + k)[0], 400)  # a unit inside a session needs the session
        self.assertEqual(code(base + "?unit=turn&sid=nope&" + k)[0], 404)
        self.assertEqual(code(base + "?root=s%3Anope&" + k)[0], 404)
        self.assertEqual(code(base + "?m=not_a_measure&" + k)[0], 400)
        self.assertEqual(code(base + "?m=none&" + k)[1]["facts"], [])

    def test_a_store_without_state_db_has_one_scope_and_the_crumb_tree_is_the_family(self):
        with tempfile.TemporaryDirectory() as home:
            with contextlib.redirect_stdout(io.StringIO()):
                seed_module().main(home)
            v = view.View(os.path.join(home, "breadcrumbs"))
            try:
                top, rows, facts = self.load(v, m="size,ctx_bytes,in_tok")
                self.assertFalse(top["has_db"])
                self.assertEqual([s["key"] for s in top["dims"]["scope"]], ["profile"])
                self.assertEqual({r["scope"] for r in rows.values()}, {"profile"})
                tops = [r for r in rows.values() if r["parent"] is None]
                self.assertEqual(len(tops), 9)  # eight past chats and today: no lineage, so a flat list
                self.assertEqual({r["kind"] for r in tops}, {"sess"})
                self.assertTrue({"turn", "step", "tool", "group"} <= {r["kind"] for r in rows.values()})
                self.assertEqual({f[3] for f in facts}, {"size", "ctx_bytes"}, "state.db measures have no cell without state.db")
                has = {m["key"]: m["has"] for m in top["dims"]["measure"]}
                self.assertEqual((has["in_tok"], has["cost_usd"], has["size"], has["ctx_bytes"], has["ace_sent"], has["u_prompt"]),
                                 (False, False, True, True, False, False))
                sess = next(r for r in tops if r["sid"] == "today")
                total = next(f for f in top["facts"] if f[0] == sess["key"] and f[3] == "size" and f[1] == 0)
                crumb = v.crumb_or_none(sess["ref"])
                self.assertTrue(crumb and total[6] == ["store", sess["ref"], "size"])
                self.assertEqual(sum(f[5] for f in top["facts"] if f[0] == sess["key"] and f[3] == "size"), v.zoom.size(sess["ref"]))
            finally:
                v.close()

    def test_the_size_of_a_crumb_is_its_own_bytes_plus_its_members_to_the_byte(self):
        today = next(r for r in self.v.measures(m="none")["dims"]["family"] if r["sid"] == "today")
        for kw in ({}, {"unit": "turn", "sid": "today"}):
            top = self.v.measures(m="size", **kw)
            row = next(r for r in top["dims"]["family"] if r["sid"] == "today")
            self.assertEqual(sum(f[5] for f in top["facts"] if f[0] == row["key"]), self.v.zoom.size(today["ref"]))
        _, rows, facts = self.load(unit="turn", sid="today", m="size")
        for r in rows.values():
            if r["kind"] != "group":
                self.assertEqual(sum(f[5] for f in facts if f[0] == r["key"]), self.v.zoom.size(r["ref"]), r["label"])
        self.assertEqual(max(r["depth"] for r in rows.values()), 1)

    def test_the_context_events_put_their_bytes_on_the_crumbs_that_they_named(self):
        sid = "today"
        chain = self.v._receipts(sid)
        want = sum(b for _, c in chain for b in c.meta["b"])
        top, rows, facts = self.load(unit="event", sid=sid, m="ctx_bytes")
        self.assertEqual([c["ev"] for c in top["dims"]["context"]], [c.meta["ev"] for _, c in chain])
        self.assertEqual([c["rcpt"] for c in top["dims"]["context"]], [rid for rid, _ in chain])
        root = next(r for r in top["dims"]["family"])
        self.assertEqual(sum(f[5] for f in facts if f[0] == root["key"]), want)
        group = next(r for r in rows.values() if r["kind"] == "group")
        self.assertEqual(group["label"], "from other sessions")
        kids = [r for r in rows.values() if r["parent"] == group["key"]]
        self.assertTrue(kids and any(r["ref"] for r in kids))
        self.assertTrue(all(r["ref"] or r["kind"] == "text" for r in kids), "a text range has no crumb to open")
        for f in facts:  # the source of a cell is a receipt of this chat
            if f[6]:
                self.assertEqual(f[6][0], "rcpt")
                self.assertTrue(all(self.v.crumb_or_none(i).kind == "rcpt" for i in f[6][1]))
        by_turn = self.v.measures(unit="turn", sid=sid, m="ctx_bytes")
        self.assertEqual(sum(f[5] for f in by_turn["facts"] if f[0] == root["key"]), want)
        _, _, deep = self.load(unit="turn", sid=sid, m="ctx_bytes")  # every row of the tree: the cells with a source are below the roots
        named = [f[6] for f in deep if f[6]]
        for src in named:  # a cell names each receipt that it sums once, and no more receipts than items
            self.assertEqual(len(set(src[1])), len(src[1]))
            self.assertLessEqual(len(src[1]), min(src[2], view.RCPT_IDS))
        two = [src for src in named if src[2] > 1]
        self.assertTrue(two, "a crumb that two events of one turn named is one cell that sums two items")
        self.assertTrue(all(len(src[1]) == src[2] == 2 for src in two), two)
        self.assertEqual({getattr(self.v.crumb_or_none(i), "kind", None) for src in two for i in src[1]}, {"rcpt"})

    def test_state_db_numbers_sit_where_the_session_starts_and_say_where_they_were_read(self):
        top = self.v.measures(m="in_tok,out_tok,messages")
        starts = {c["i"]: (c["t0"], c["t1"]) for c in top["dims"]["context"]}
        rows = {r["sid"]: r for r in top["dims"]["family"]}
        f = {(x[0], x[3]): x for x in top["facts"] if x[4] is not None}  # a row's own cells (the others are incl only)
        today = f[(rows["today"]["key"], "in_tok")]
        self.assertEqual((today[4], today[6]), (52300, ["db", "sessions.input_tokens", "today"]))
        a, b = starts[today[1]]
        self.assertTrue(a <= self.fx["t"]["today"][0] < b)
        # inclusive: a session and what it includes, each where it starts. The two helpers start in the same hour as
        # the chat; its continuation and the helper of that start hours later, so they add to later columns
        self.assertEqual(today[5], 52300 + 7000 + 9000)
        later = sorted((x[1], x[5]) for x in top["facts"] if x[0] == rows["today"]["key"] and x[3] == "in_tok" and x[4] is None)
        self.assertEqual([v for _, v in later], [33000, 4000], "the continuation, then its helper, in their own hours")
        self.assertTrue(all(c > today[1] for c, _ in later))
        self.assertEqual(sum(x[5] for x in top["facts"] if x[0] == rows["today"]["key"] and x[3] == "in_tok"),
                         52300 + 7000 + 9000 + 33000 + 4000)
        self.assertIsNone(f.get((rows["ghost"]["key"], "tool_calls")))  # not asked for
        self.assertEqual(rows["today-h1"]["how"], "subagent")
        self.assertEqual(rows["today-h1"]["parent"], rows["today"]["key"])
        self.assertEqual(rows["today-cont-h"]["parent"], rows["today-cont"]["key"])
        self.assertEqual([r["depth"] for r in (rows["today"], rows["today-h1"], rows["today-cont-h"])], [0, 1, 2])
        # the numbers do not sit in a unit that cannot place them
        self.assertEqual(self.v.measures(unit="turn", sid="today", m="in_tok")["facts"], [])
        self.assertEqual(self.v.measures(unit="request", sid="today", m="in_tok")["facts"], [])
        units = {m["key"]: m["units"] for m in top["dims"]["measure"]}
        self.assertEqual((units["in_tok"], units["u_prompt"], units["ctx_bytes"], units["size"]),
                         (["time"], ["time", "request"], ["time", "turn", "event"], ["time", "turn"]))

    def test_cost_keeps_its_grain_model_and_task_are_keys_and_unknown_is_not_zero(self):
        top = self.v.measures(m="cost_usd")
        rows = {r["sid"]: r for r in top["dims"]["family"]}
        mine = [f for f in top["facts"] if f[0] == rows["today"]["key"]]
        own_facts = [f for f in mine if f[4] is not None]  # the others are cells of the family below it: incl only
        self.assertEqual(sorted((f[7], f[8], f[4]) for f in own_facts),
                         sorted([(SONNET, "main", 1.2345), (OPUS, "main", 3.1), (HAIKU, "title_generation", 0.0021),
                                 (HAIKU, "compression", 0.01)]))
        for f in own_facts:
            self.assertEqual(f[6][:3], ["db", "session_model_usage", "today"])
            self.assertEqual(f[6][3:6], [f[7], "" if f[8] == "main" else f[8], "estimated_cost_usd"])
            self.assertEqual(f[6][6:], ["estimated", "provider_models_api" if f[7] != HAIKU else "official_docs_snapshot"])
        # inclusive: the sum of the family's recorded rows, each at its own model's cost, by model
        by_model = {}
        for f in mine:
            by_model[f[7]] = by_model.get(f[7], 0) + f[5]
        self.assertAlmostEqual(by_model[SONNET], 1.2345 + 0.4 + 0.55 + 0.25, places=6)  # the actual cost wins over the estimate
        self.assertAlmostEqual(by_model[OPUS], 3.1 + 2.5, places=6)
        self.assertAlmostEqual(by_model[HAIKU], 0.0121, places=6)
        own = {f[7]: f[4] for f in own_facts if f[8] == "main"}
        self.assertEqual(own, {SONNET: 1.2345, OPUS: 3.1})
        child = [f for f in top["facts"] if f[0] == rows["today-cont-h"]["key"]]
        self.assertEqual([(f[4], f[6][5]) for f in child], [(0.25, "actual_cost_usd")])
        # recorded zero (included in a subscription) is a cell; zero cost with an unknown or no status is no data
        self.assertEqual([f[4] for f in top["facts"] if f[0] == rows["tea"]["key"]], [0.0])
        self.assertEqual([f for f in top["facts"] if f[0] in (rows["bike"]["key"], rows["bike-2"]["key"])], [])
        self.assertEqual(top["dims"]["model"], [HAIKU, OPUS, SONNET])
        self.assertTrue(all(m["keyed"] for m in top["dims"]["measure"] if m["key"] in ("cost_usd", "u_prompt")))
        # the family label never holds the model
        self.assertFalse([r for r in top["dims"]["family"] if any(m in r["label"] for m in (SONNET, OPUS, HAIKU))])
        # one number a (session, model, task): none is computed from tokens
        self.assertEqual(len(own_facts), 4)

    def test_scope_is_the_codebase_and_branch_of_the_session_and_a_filter_keeps_only_its_sessions(self):
        top = self.v.measures(m="in_tok")
        by = {s["key"]: s for s in top["dims"]["scope"]}
        self.assertEqual(by["profile"]["n"], 14)
        self.assertEqual((by["cb:" + REPO_A]["n"], by["cb:" + REPO_B]["n"], by["cb:none"]["n"]), (9, 4, 1))
        self.assertEqual(by["br:%s|feature/kettle" % REPO_A]["parent"], "cb:" + REPO_A)
        self.assertEqual([s["kind"] for s in top["dims"]["scope"]][:3], ["profile", "codebase", "branch"])
        rows = {r["sid"]: r for r in top["dims"]["family"]}
        self.assertEqual(rows["trip"]["scope"], "br:none|none")  # a cwd that is in no repository: none
        self.assertEqual(rows["bike"]["scope"], "br:%s|fix/cassette" % REPO_B)
        b = self.v.measures(m="in_tok", codebase=REPO_B)
        self.assertEqual({r["sid"] for r in b["dims"]["family"]}, {"bike", "bike-2", "parser", "ghost"})
        self.assertEqual(sum(t[2] for t in b["totals"]), 1200 + 1300 + 1400 + 2500)  # the in_tok of those four sessions
        self.assertEqual(len(b["dims"]["scope"]), len(top["dims"]["scope"]), "the chips stay when one is chosen")
        k = self.v.measures(m="in_tok", codebase=REPO_A, branch="feature/kettle")
        rows = k["dims"]["family"]
        self.assertEqual({r["sid"] for r in rows}, {"today-h1", "today-h2"})
        self.assertEqual([r["parent"] for r in rows], [None, None], "a session whose parent is out of scope is a root")
        self.assertEqual(sum(t[2] for t in k["totals"]), 16000)

    def test_a_long_span_gets_day_buckets_and_rows_are_capped_with_exact_totals(self):
        with tempfile.TemporaryDirectory() as home:
            fx = build_fixture(home, far_days=5)
            sessions = [{"id": "x%03d" % i, "t0": fx["far"] + 60 * i, "t1": fx["far"] + 60 * i + 30, "in": 10} for i in range(320)]
            os.remove(os.path.join(home, "state.db"))
            state_db(os.path.join(home, "state.db"), sessions + [
                {"id": "today", "t0": fx["t"]["today"][0], "t1": fx["t"]["today"][1], "in": 52300}], [])
            v = view.View(fx["root"])
            try:
                top = v.measures(m="in_tok")
                self.assertEqual(top["bucket"], "day")
                self.assertGreaterEqual(len(top["dims"]["context"]), 6)
                self.assertTrue(all(c["t1"] > c["t0"] for c in top["dims"]["context"]))
                self.assertEqual(top["capped"]["rows"], True)
                self.assertEqual((len(top["dims"]["family"]), top["capped"]["total_rows"], top["capped"]["limit"]), (300, 329, 300))
                self.assertEqual(sum(t[2] for t in top["totals"]), 320 * 10 + 52300 + 0)
                self.assertEqual(len(v.measures(m="in_tok", limit=5)["dims"]["family"]), 5)
            finally:
                v.close()

    def test_usage_log_lines_are_read_in_the_fixed_format(self):
        with tempfile.TemporaryDirectory() as root:
            good = usage_log_line(1794000000, "s-one", OPUS, 5000, cr=3000, cw=100, out=70, rsn=9, fin="end_turn", turn="t1", req="r9")
            old = usage_log_line(1793999000, "s-one", SONNET, 4000)
            lines = [good,
                     "2026-11-10T15:40:12 sess=s-two turn=- req=- n=2 task=approval model=- resp=- prov=- in=0 cr=0 cw=0 out=0 "
                     "rsn=0 prompt=0 est=77 msgs=3 tools=1 dur=- fin=error\n",
                     "garbage\n", "2026-11-10T15:40:12 pid=1 act=pass\n",
                     usage_log_line(1794000100, "s-one", OPUS, 1)[:-1]]  # no line end: not a whole line
            for name, body in (("usage.log.1", old), ("usage.log", "".join(lines))):
                with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                    f.write(body)
            rows = view.usage_requests(root)
            self.assertEqual([(r["sess"], r["model"], r["prompt"], r["file"], r["no"]) for r in rows],
                             [("s-one", SONNET, 4000, "usage.log.1", 1), ("s-one", OPUS, 5000, "usage.log", 1),
                              ("s-two", None, 0, "usage.log", 2)], "the older file first; bad lines skipped")
            r = rows[1]
            self.assertEqual((r["in"], r["cr"], r["cw"], r["out"], r["rsn"], r["est"], r["task"], r["turn"], r["req"], r["fin"]),
                             (1900, 3000, 100, 70, 9, 5000, "main", "t1", "r9", "end_turn"))
            self.assertEqual(r["line"], good.rstrip("\n"))
            self.assertEqual((rows[2]["task"], rows[2]["fin"], rows[2]["resp"], rows[2]["dur"], rows[2]["n"]), ("approval", "error", None, 0.0, 2))
            self.assertEqual(view.usage_requests(os.path.join(root, "none")), [])

    def test_the_context_line_follows_the_provider_count_with_the_model_and_the_line_it_came_from(self):
        ans = self.v.measures(unit="request", sid="today", lines=True, m="none")
        s = ans["lines"]["series"][0]
        self.assertEqual((s["role"], s["sid"], s["n"], s["n_aux"]), ("focus", "today", 8, 2))
        self.assertEqual([p["prompt"] for p in s["points"]], [460, 760, 1050, 760, 860, 1010, 1070, 940])
        self.assertEqual([p["model"] for p in s["points"]], [SONNET] * 5 + [OPUS] * 3, "a chat that went from Sonnet to Opus")
        self.assertEqual([p["x"] for p in s["points"]], [i + 0.5 for i in range(8)], "a request is a column")
        self.assertEqual([p["x"] for p in s["ace"]], [i + 0.5 for i in range(8)], "ACE's line is on the same columns")
        self.assertEqual([p["act"] for p in s["ace"]], ["pass", "pass", "pass", "fold", "keep", "keep", "keep", "fold"])
        self.assertEqual([(p["sent"], p["target"]) for p in s["ace"]][3], (700, 1000))
        with open(os.path.join(self.root, "usage.log"), encoding="utf-8") as fh:
            text = fh.read().split("\n")
        for p in s["points"]:
            self.assertIn(p["line"], text)  # the provenance is the line itself
            self.assertTrue(p["line"].startswith(stamp(p["ts"])))
        self.assertEqual({(p["task"], p["model"]) for p in s["aux"]}, {("title_generation", HAIKU), ("compression", HAIKU)})
        self.assertTrue(all(0 <= p["x"] <= 8 for p in s["aux"]))
        self.assertEqual([p["in"] + p["cr"] + p["cw"] for p in s["points"]], [p["prompt"] for p in s["points"]])
        self.assertEqual(ans["dims"]["model"], [HAIKU, OPUS, SONNET])
        # on the time axis the same points sit where they happened
        t = self.v.measures(unit="time", sid="today", lines=True, m="none")
        xs = [p["x"] for p in t["lines"]["series"][0]["points"]]
        self.assertEqual(xs, sorted(xs))
        hour = t["dims"]["context"][int(xs[0])]
        self.assertTrue(hour["t0"] <= t["lines"]["series"][0]["points"][0]["ts"] < hour["t1"])
        self.assertAlmostEqual(xs[0] % 1, (t["lines"]["series"][0]["points"][0]["ts"] - hour["t0"]) / 3600.0, places=3)
        # on the turn axis a point sits between the turns around its time
        n = self.v.measures(unit="turn", sid="today", lines=True, m="none")["lines"]["series"][0]["points"]
        self.assertEqual([p["x"] for p in n], sorted(p["x"] for p in n))
        self.assertLess(n[-1]["x"], 5)

    def test_inclusive_adds_one_line_for_each_session_under_the_focus(self):
        one = self.v.measures(unit="time", sid="today", lines=True, m="none")["lines"]
        many = self.v.measures(unit="time", sid="today", lines=True, incl=True, m="none")["lines"]
        self.assertEqual([s["sid"] for s in one["series"]], ["today"])
        self.assertEqual(sorted(s["sid"] for s in many["series"]), ["today", "today-cont", "today-cont-h", "today-h1", "today-h2"])
        self.assertEqual([s["role"] for s in many["series"]], ["focus"] + ["member"] * 4)
        self.assertEqual((many["members"], many["shown"]), (4, 4))
        depth = {s["sid"]: s["depth"] for s in many["series"]}
        self.assertEqual((depth["today-h1"], depth["today-cont"], depth["today-cont-h"]), (1, 1, 2))
        self.assertEqual(len(set(s["label"] for s in many["series"])), 5, "each line has its own label")
        cap = self.v.measures(unit="time", sid="today", lines=True, incl=True, m="none")
        self.assertLessEqual(len(cap["lines"]["series"]), view.LINES_MAX)
        # no unit but time has the sessions under the focus on its axis
        self.assertEqual(len(self.v.measures(unit="request", sid="today", lines=True, incl=True, m="none")["lines"]["series"]), 1)

    def test_usage_and_ace_measures_are_sums_of_lines_in_a_cell_and_keyed_by_model(self):
        top = self.v.measures(m="u_prompt,ace_sent,ace_target,ace_folds")
        rows = {r["sid"]: r for r in top["dims"]["family"]}
        f = [x for x in top["facts"] if x[0] == rows["today"]["key"] and x[4] is not None]  # its own cells
        prompts = {(x[7], x[8]): x[4] for x in f if x[3] == "u_prompt"}
        self.assertEqual(prompts[(SONNET, "main")], 460 + 760 + 1050 + 760 + 860)
        self.assertEqual(prompts[(OPUS, "main")], 1010 + 1070 + 940)
        self.assertEqual(prompts[(HAIKU, "compression")], 2400)
        src = next(x[6] for x in f if x[3] == "u_prompt" and x[7] == SONNET)
        self.assertEqual(src[:3], ["usage", "today", 5])
        self.assertEqual([x[4] for x in f if x[3] == "ace_sent"], [400 + 700 + 990 + 700 + 800 + 950 + 1010 + 880])
        self.assertEqual([x[4] for x in f if x[3] == "ace_target"], [1000], "a level is the largest in the cell, not a sum")
        self.assertEqual([x[4] for x in f if x[3] == "ace_folds"], [2])
        self.assertEqual(next(x[6] for x in f if x[3] == "ace_sent")[:3], ["ace", "today", 8])
        # a request is a cell of its own on the request axis
        r = self.v.measures(unit="request", sid="today", m="ace_sent,u_prompt")
        sent = {x[1]: x[4] for x in r["facts"] if x[3] == "ace_sent"}
        self.assertEqual([sent[i] for i in range(8)], [400, 700, 990, 700, 800, 950, 1010, 880])
        self.assertEqual([c["act"] for c in r["dims"]["context"]][3], "fold")
        # the two counts of a request are in one column (ACE's estimate, the provider's count)
        got = {(x[1], x[3]): x[4] for x in r["facts"] if x[0] == r["dims"]["family"][0]["key"]}
        self.assertEqual([got[(i, "u_prompt")] - got[(i, "ace_sent")] for i in range(8)], [60] * 8)

    def test_the_request_axis_pairs_an_ace_line_with_the_usage_line_that_follows_it(self):
        ace = [{"ts": 10, "n": "a1"}, {"ts": 20, "n": "a2"}, {"ts": 40, "n": "a3"}]
        use = [{"ts": 11, "n": "u1"}, {"ts": 12, "n": "u1b"}, {"ts": 41, "n": "u3"}, {"ts": 90, "n": "u4"}]
        slots = view.request_slots(ace, use)
        self.assertEqual([(s["ace"] and s["ace"]["n"], s["usage"] and s["usage"]["n"]) for s in slots],
                         [("a1", "u1"), (None, "u1b"), ("a2", None), ("a3", "u3"), (None, "u4")])
        self.assertEqual([s["ts"] for s in slots], [10, 12, 20, 40, 90])
        self.assertEqual(view.request_slots([], use)[0]["ace"], None)
        self.assertEqual(len(view.request_slots(ace, [])), 3)

    def test_the_time_axis_is_linear_and_a_time_has_a_place_on_it(self):
        kind, starts = view.time_buckets(1794000000, 1794000000 + 5 * 3600 + 5)
        self.assertEqual((kind, len(starts)), ("hour", 6 if (1794000000 % 3600) + 5 < 3600 else 7))
        self.assertTrue(all(b - a == 3600 for a, b in zip(starts, starts[1:])))
        kind, starts = view.time_buckets(1794000000, 1794000000 + 10 * 86400)
        self.assertEqual((kind, len(starts)), ("day", 11))
        self.assertTrue(all(86400 - 3600 <= b - a <= 86400 + 3600 for a, b in zip(starts, starts[1:])))
        starts = [0, 10, 20]
        got = [view.place(t, starts, 30) for t in (-5, 0, 5, 10, 29.99, 40)]
        for a, b in zip(got, [0.0, 0.0, 0.5, 1.0, 2.999, 2.999]):
            self.assertAlmostEqual(a, b, places=6)

    def test_the_first_view_reads_no_crumb_tree_no_text_and_no_cue_word(self):
        v = view.View(self.root)  # a new View: no tree, no size, no cue word is held yet

        def boom(*a, **k):
            raise AssertionError("the first view read a crumb tree or a text")

        built, real_walk = [], view._Walk
        view._Walk = lambda *a, **k: built.append(a[1]) or real_walk(*a, **k)
        self.addCleanup(setattr, view, "_Walk", real_walk)
        self.addCleanup(v.close)
        shadows = ("size", "full", "handle", "cue_words", "source", "render", "_doc_lines")
        try:
            for name in shadows:
                setattr(v.zoom, name, boom)  # an attribute of the instance hides the method
            for kw in ({}, {"incl": True}, {"unit": "event", "sid": "today"}, {"unit": "request", "sid": "today"}):
                ans = v.measures(**kw)
                self.assertTrue(ans["dims"]["family"] and ans["facts"], kw)
                self.assertFalse([f for f in ans["facts"] if f[3] == "size"], "the size is read from the trees: it is asked for")
            self.assertEqual({r["kind"] for r in v.measures()["dims"]["family"]}, {"sess"})
            self.assertEqual(built, [], "no crumb tree was walked")
        finally:
            for name in shadows:
                if name in vars(v.zoom):
                    delattr(v.zoom, name)
        # the turns of one chat are its trees' leaves: the Turn unit walks that chat, and no other
        ans = v.measures(unit="turn", sid="today")
        self.assertEqual(built, [next(r["ref"] for r in ans["dims"]["family"] if r["sid"] == "today")])

    def test_the_first_view_has_the_same_facts_as_the_full_answer_for_the_rows_it_shows(self):
        strip = lambda rows: [{k: x for k, x in r.items() if k not in ("label", "named")} for r in rows]  # noqa: E731
        asked = 0
        for kw in self.UNITS:
            first, full = self.v.measures(**kw), self.v.measures(m="all", **kw)
            self.assertEqual(first["facts"], [f for f in full["facts"] if f[3] != "size"], kw)
            self.assertEqual(first["totals"], [t for t in full["totals"] if t[0] != "size"], kw)
            self.assertEqual(strip(first["dims"]["family"]), strip(full["dims"]["family"]), kw)
            for part in ("scope", "context", "model"):
                self.assertEqual(first["dims"][part], full["dims"][part], (kw, part))
            for key in ("unit", "bucket", "focus", "capped", "profile", "has_db"):
                self.assertEqual(first[key], full[key], (kw, key))
            asked += len([f for f in full["facts"] if f[3] == "size"])
            if "unit" not in kw:  # the rows that open: the same facts, the first view or not
                for r in first["dims"]["family"]:
                    if r["more"]:
                        self.assertEqual(self.v.measures(root=r["key"])["facts"],
                                         [f for f in self.v.measures(root=r["key"], m="all")["facts"] if f[3] != "size"], r["key"])
        self.assertGreater(asked, 0, "the fixture has sizes to leave out")

    def test_no_m_leaves_out_the_measures_that_walk_and_all_asks_for_every_measure(self):
        self.assertEqual({m["key"] for m in view.MEASURES if m["walks"]}, {"size"})
        self.assertEqual({m["key"]: m["walks"] for m in self.v.measures(m="none")["dims"]["measure"]},
                         {m["key"]: m["key"] == "size" for m in view.MEASURES})
        first = {f[3] for f in self.v.measures()["facts"]}
        everything = {f[3] for f in self.v.measures(m="all")["facts"]}
        self.assertEqual(everything - first, {"size"})
        self.assertLessEqual(first, set(view.MEASURE) - {"size"})
        self.assertEqual({f[3] for f in self.v.measures(m="size")["facts"]}, {"size"})
        self.assertEqual({f[3] for f in self.v.measures(m="in_tok,all")["facts"]}, everything)
        self.assertEqual({f[3] for f in self.v.measures(m="in_tok,size")["facts"]}, {"in_tok", "size"})
        self.assertEqual(self.v.measures(m="none")["facts"], [])
        with self.assertRaises(ValueError):
            self.v.measures(m="in_tok,nope")

    def test_a_chat_has_a_quick_label_until_its_full_label_is_asked_for(self):
        rows = self.v.measures(m="none")["dims"]["family"]
        with_ref = [r for r in rows if r["ref"]]
        self.assertTrue(with_ref and len(with_ref) < len(rows), "some chats are in the store, one is only in state.db")
        for r in rows:
            if r["ref"]:
                self.assertFalse(r["named"], r["sid"])
                self.assertRegex(r["label"], r"^sess \d\d-\d\d \d\d:\d\d$", "the quick label is the start time")
            else:
                self.assertEqual((r["named"], r["label"]), (True, view.short_sid(r["sid"])))
        want = {r["ref"]: view.label(self.v.zoom, r["ref"]) for r in with_ref}  # the label as it was: the kind, then three cue words
        self.assertTrue(all(x.startswith("sess ") and x != "sess " for x in want.values()))
        self.assertNotEqual(want[with_ref[0]["ref"]], with_ref[0]["label"])
        self.assertEqual(self.v.labels(list(want)), want)
        named = self.v.measures(m="none", names=True)["dims"]["family"]
        self.assertEqual([(r["label"], r["named"]) for r in named if r["ref"]], [(want[r["ref"]], True) for r in named if r["ref"]])
        self.assertEqual([r["label"] for r in named if not r["ref"]], [r["label"] for r in rows if not r["ref"]])
        self.assertEqual(self.v.labels(["00000000", "nope"]), {"00000000": "00000000", "nope": "nope"}, "a ref the store lacks is its own label")
        self.assertEqual(len(self.v.labels(["%08x" % i for i in range(view.LABELS_MAX + 5)])), view.LABELS_MAX)
        # a lines graph names its lines by the short id until the full label is asked for
        series = self.v.measures(unit="time", sid="today", lines=True, m="none")["lines"]["series"]
        self.assertEqual(series[0]["label"], "sess " + view.short_sid("today"))
        series = self.v.measures(unit="time", sid="today", lines=True, m="none", names=True)["lines"]["series"]
        self.assertEqual(series[0]["label"], want[next(r["ref"] for r in with_ref if r["sid"] == "today")])

    def test_the_quick_label_of_a_crumb_is_the_title_as_the_full_label_gives_it_without_cue_words(self):
        q = view._Measures(self.v, [], "time", "", set(), "", "", False, False, 300)
        turns = [r for r in self.load(m="none")[1].values() if r["kind"] == "turn"]
        self.assertTrue(turns)
        for r in turns:  # a kind without cue words: the quick label and the label are one
            c = self.v.crumb_or_none(r["ref"])
            self.assertEqual(q.quick("turn", c.title, r["ref"]), view.label(self.v.zoom, r["ref"]))
            self.assertEqual((r["named"], r["label"]), (True, view.label(self.v.zoom, r["ref"])))
        self.assertEqual(q.quick("span", "", "abcd1234"), "span abcd1234")
        self.assertEqual(q.quick("span", "x" * 80, "abcd1234"), "span " + "x" * 28)
        self.assertEqual(q.quick("span", 'say "hi"', "abcd1234"), "span say \\", "cut at the first quote, as label() cuts the JSON of a title")

    def test_the_labels_route_needs_the_token_and_gives_the_full_labels(self):
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        refs = [r["ref"] for r in self.v.measures(m="none")["dims"]["family"] if r["ref"]][:3]
        url = "http://127.0.0.1:%d/api/measures/labels?refs=%s" % (server.server_address[1], "|".join(refs))
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        with urllib.request.urlopen(url + "&k=" + view.token(self.root)) as r:
            self.assertEqual(json.load(r), {ref: view.label(self.v.zoom, ref) for ref in refs})
        with urllib.request.urlopen("http://127.0.0.1:%d/api/measures/labels?k=%s" % (server.server_address[1], view.token(self.root))) as r:
            self.assertEqual(json.load(r), {}, "no refs, no labels")

    def test_a_day_after_the_31st_is_a_real_day_and_mktime_never_gets_day_32(self):
        # GraalPy 25 raises a Java DateTimeException (and the interpreter ends) for time.mktime((y, m, 32, ...)); CPython reads it
        # as day 1 of the next month. So the test spies on mktime: no day that the month does not have may reach it, on any Python.
        real, seen = time.mktime, []

        def spy(fields):
            seen.append(tuple(fields[:3]))
            self.assertTrue(1 <= fields[2] <= calendar.monthrange(fields[0], fields[1])[1], fields)
            return real(fields)

        def noon(y, m, d):
            return real((y, m, d, 12, 0, 0, 0, 0, -1))

        def midnight(y, m, d):
            return real((y, m, d, 0, 0, 0, 0, 0, -1))

        cases = [((2026, 10, 31), 1, (2026, 11, 1)), ((2026, 12, 31), 1, (2027, 1, 1)), ((2026, 11, 30), 1, (2026, 12, 1)),
                 ((2028, 2, 28), 1, (2028, 2, 29)), ((2028, 2, 29), 1, (2028, 3, 1)), ((2027, 2, 28), 1, (2027, 3, 1)),
                 ((2026, 11, 1), -1, (2026, 10, 31)), ((2027, 1, 1), -1, (2026, 12, 31)), ((2026, 10, 31), 0, (2026, 10, 31)),
                 ((2026, 10, 31), 31, (2026, 12, 1))]
        time.mktime = spy
        try:
            for day, plus, want in cases:
                self.assertEqual(view._midnight(noon(*day), plus), midnight(*want), (day, plus))
            kind, starts = view.time_buckets(noon(2026, 10, 29), noon(2026, 11, 3))  # a long span over the end of October
        finally:
            time.mktime = real
        self.assertEqual(kind, "day")
        self.assertEqual(starts, [midnight(2026, 10, 29), midnight(2026, 10, 30), midnight(2026, 10, 31), midnight(2026, 11, 1),
                                  midnight(2026, 11, 2), midnight(2026, 11, 3)])
        self.assertIn((2026, 11, 1), seen)

    def test_a_row_key_that_does_not_name_a_row_is_unknown(self):
        for key in ("nope", "s:nope", "s:today>x.y", "s:today>99.ffffffff", "s:today>0.00000000", "s:today>~>0.x"):
            if key == "s:today>~>0.x":  # one crumb of the group has no members
                self.assertEqual(self.v.measures(root=key)["dims"]["family"], [])
                continue
            with self.assertRaises(KeyError, msg=key):
                self.v.measures(root=key)
        with self.assertRaises(KeyError):
            self.v.measures(unit="turn", sid="nope")
        with self.assertRaises(ValueError):
            self.v.measures(unit="week")
        with self.assertRaises(ValueError):
            self.v.measures(unit="event")
        self.assertEqual(self.v.measures(root="s:ghost")["dims"]["family"], [], "a chat the store lacks has no crumbs")

    def test_the_line_that_the_recorder_writes_is_read_field_for_field(self):
        # the sample line of tests/test_usage_log.py (breadcrumbs/usage_log.py): est and tools read 0 in real lines
        line = ("2026-11-10T15:40:12 sess=20261110_154012_a1b2c3 turn=turn-7 req=req-42 n=3 task=main model=claude-sonnet-4-6 "
                "resp=claude-sonnet-4-6-20261001 prov=anthropic in=120 cr=18000 cw=500 out=250 rsn=40 prompt=18620 est=0 msgs=41 "
                "tools=0 dur=3.412 fin=tool_calls\n"
                "2026-11-10T15:40:30 sess=20261110_154012_a1b2c3 turn=- req=- n=0 task=title_generation model=- resp=- prov=- "
                "in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 est=0 msgs=0 tools=0 dur=0.000 fin=error\n")
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "usage.log"), "w", encoding="utf-8") as f:
                f.write(line)
            main, aux = view.usage_requests(root)
        want = {"sess": "20261110_154012_a1b2c3", "turn": "turn-7", "req": "req-42", "n": 3, "task": "main",
                "model": "claude-sonnet-4-6", "resp": "claude-sonnet-4-6-20261001", "prov": "anthropic", "in": 120, "cr": 18000,
                "cw": 500, "out": 250, "rsn": 40, "prompt": 18620, "est": 0, "msgs": 41, "tools": 0, "dur": 3.412,
                "fin": "tool_calls", "file": "usage.log", "no": 1}
        self.assertEqual({k: main[k] for k in want}, want)
        self.assertEqual(main["line"], line.split("\n")[0], "the line itself is the provenance of a point")
        self.assertEqual(main["prompt"], main["in"] + main["cr"] + main["cw"], "prompt is the context that the provider counted")
        self.assertEqual((aux["task"], aux["model"], aux["turn"], aux["fin"], aux["prompt"]), ("title_generation", None, None, "error", 0))
        self.assertEqual(view.short_sid(main["sess"]), "154012_a1b2c3")

    def test_only_the_named_numeric_columns_are_read_and_never_the_messages(self):
        banned = {"title", "display_name", "system_prompt", "last_activity_description", "origin_json", "messages", "content"}
        for name in (view.SESSION_COLS + view.MODEL_USAGE_COLS):
            self.assertNotIn(name, banned)
            self.assertFalse(name.startswith("handoff_") or name.endswith("_url") or name == "billing_base_url", name)
        src = inspect.getsource(view.read_state)
        self.assertNotRegex(src, r"(?i)from\s+messages")
        self.assertEqual(sorted(re.findall(r"FROM (\w+)", src)), ["session_model_usage", "sessions"])
        self.assertIn("estimated_cost_usd", view.MODEL_USAGE_COLS)
        self.assertNotIn("input_tokens", view.MODEL_USAGE_COLS, "cost stays at the grain that it is recorded; no tokens or prices are read for it")


class SpanLabelsTest(ViewData):
    """A chat of more than 16 turns has span crumbs (ingest.put_tree). The label of a span has cue words, which cost a read of all
    the text under it, so the row of a span has a quick label (its title) and named False until View.labels gives the full one."""

    def test_a_span_row_has_a_quick_label_and_the_crumbs_below_it_have_the_full_one(self):
        for i in range(40):
            self.p.sync_turn("question %d about the kettle and the green tea" % i, "answer %d: steep the tea in water" % i, session_id="long")
        self.p.shutdown()
        v = self.open()
        row = next(r for r in v.measures(m="none")["dims"]["family"] if r["sid"] == "long")
        self.assertTrue(row["more"] and not row["named"])
        kids = v.measures(root=row["key"], m="none")["dims"]["family"]
        spans = [r for r in kids if r["kind"] == "span"]
        self.assertTrue(spans, "a chat with 40 turns has spans")
        for r in spans:
            self.assertEqual(r["named"], False)
            self.assertEqual(r["label"], "span " + v.crumb_or_none(r["ref"]).title[:28])
        full = {r["ref"]: view.label(v.zoom, r["ref"]) for r in spans}
        self.assertTrue(all(x != r["label"] for x, r in zip(full.values(), spans)), "cue words, not the title")
        self.assertEqual(v.labels(list(full)), full)
        asked = v.measures(root=row["key"], m="none", names=True)["dims"]["family"]
        self.assertEqual([(r["label"], r["named"]) for r in asked if r["kind"] == "span"], [(full[r["ref"]], True) for r in spans])
        below = v.measures(root=spans[0]["key"], m="none")["dims"]["family"]
        self.assertTrue(below)
        self.assertEqual([(r["label"], r["named"]) for r in below if r["kind"] not in ("span", "sess")],
                         [(view.label(v.zoom, r["ref"]), True) for r in below if r["kind"] not in ("span", "sess")], "a turn has no cue words")


class WalkSizeTest(ViewData):
    """The sizes of a walk (selfb of each crumb) are Zoom.size's to the byte, though the walk reads no line of text for them: the
    store's index holds the length of each line. The oracle is a Zoom of the test's own: nothing that the View computed is trusted."""

    def build(self):
        for i in range(40):  # more than 16 turns: spans
            self.p.sync_turn("question %d about the kettle and the green tea" % i, "answer %d: steep the tea in water\nand wait" % i, session_id="long")
        self.p.sync_turn("bin\x00ary question\nsecond line\x00 with a nul\n" + "x" * 600, "a\x00b", session_id="binary")  # a text with a NUL: binary
        self.p.sync_turn("a question after it", "an answer after it", session_id="binary")
        self.p.shutdown()
        return self.open()

    def test_the_sizes_of_a_walk_are_zoom_sizes_to_the_byte(self):
        v = self.build()
        z = view.Zoom(v.store, summaries=v.index.summary_of, cues=v.index.signature, words=v.index.tiers)
        seen = set()
        for name in v.refs.names():
            if not name.startswith("sess-"):
                continue
            ref = v.refs.get(name)
            w = v.walk(ref)
            want = [z.size(cid) - sum(z.size(w.refs[j]) for j in w.kids[i]) for i, cid in enumerate(w.refs)]
            self.assertEqual(w.selfb, want, name)
            self.assertEqual(sum(w.selfb), z.size(ref), name)
            seen |= {c.kind for c in (v.crumb_or_none(x) for x in w.refs) if c}
        self.assertLessEqual({"sess", "span", "turn"}, seen)
        binary = v.refs.get(next(n for n in v.refs.names() if n.startswith("sess-") and "binary" in n))
        self.assertIn("(binary,", z.render(binary, 1.0), "the fixture has a binary text")
        self.assertNotIn("(binary,", z.render(v.refs.get(next(n for n in v.refs.names() if "long" in n)), 1.0))

    def test_a_walk_reads_crumbs_and_spines_and_no_line_of_text(self):
        v = self.build()
        got = []
        real = v.store.get
        v.store.get = lambda blob_id: got.append(real(blob_id)[:4]) or real(blob_id)
        self.addCleanup(setattr, v.store, "get", real)
        v.walk(v.refs.get(next(n for n in v.refs.names() if "long" in n)))
        self.assertTrue(got and set(got) <= {b"CR01", b"SP01"}, set(got))
        self.assertIn(b"SP01", got)


class MeasuresPageTest(unittest.TestCase):
    """breadcrumbs/view.html: the Measures panel. A browser is not needed here: the page text, and (when node is installed) its
    script is parsed. The panel was also driven in headless Chrome (clicks, wheel, a thrown drag, #motion=0, 300 rows)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "breadcrumbs", "view.html"), encoding="utf-8") as f:
            cls.page = f.read()

    def test_the_toolbar_has_the_toggle_beside_context_and_size(self):
        i = [self.page.index('id="%s"' % k) for k in ("ctxon", "trkon", "meaon", "follow")]
        self.assertEqual(i, sorted(i), "Context, Size, Measures, then Follow the agent")
        self.assertIn("#measures=1", self.page)
        self.assertRegex(self.page, r'\$\("ctxon"\)\.checked = HASH\.get\("context"\) === "1"')  # the old toggles keep their links

    def test_the_page_asks_for_the_full_labels_after_the_rows_and_keeps_them(self):
        for part in ('api("measures/labels"', "MEA_NAMES", "r.named === false", "!MEA.loading", "m.walks"):
            self.assertIn(part, self.page)
        self.assertLess(self.page.index("meaNames();\n  } catch (e) { if (seq === MEA.seq)"), self.page.index("async function meaMembers"),
                        "meaLoad asks for the labels when its rows are in")

    def test_each_picker_says_what_it_does_and_the_empty_panel_says_what_to_pick(self):
        for key in ("mem", "meu", "mei", "mes", "mesc"):
            m = re.search(r'<[^>]*title="([^"]+)"[^>]*>(?:(?!</div>).)*?id="%s"' % key, self.page, re.S)
            self.assertTrue(m and len(m.group(1)) > 40, key)
        self.assertIn("Pick a measure, then click a chat row to chart its context size.", self.page)
        self.assertIn("No chats in this scope. Pick another scope chip.", self.page)

    def test_the_panel_uses_the_spring_of_the_map_and_does_not_replace_it(self):
        for name in ("spring(", "MOVE.focus", "MOVE.wheel", "MOVE.throw", "MOVE.flick", "MOVE.fast", "MOVE.hand", "MOTION"):
            self.assertIn(name, self.page.split("// -- measures: a matrix")[1], name)
        self.assertEqual(self.page.count("function damp("), 1)
        self.assertEqual(self.page.count("function spring("), 1)
        self.assertEqual(self.page.count("const MOVE = {"), 1)

    def test_the_chart_names_the_colours_of_its_model_band_in_its_title_row(self):
        mine = self.page.split("// -- measures: a matrix")[1]
        chart = mine.split("function meaChart(")[1].split("\n}\n")[0]
        self.assertIn("meaModelKey(", chart, "the key is drawn by the chart, not only in the footer")
        self.assertIn("meaModelColor(", mine.split("function meaModelKey(")[1].split("\n}\n")[0], "the key has the colour of the band")
        self.assertIn("meaModelColor(p.model)", mine.split("the model band: under the axis")[1].split("\n}\n")[0], "the band")

    def test_a_number_is_drawn_only_when_the_plot_shows_all_of_it(self):
        mine = self.page.split("// -- measures: a matrix")[1]
        self.assertEqual(mine.count("function meaFits("), 1)
        body = mine.split("function meaBody(")[1].split("\nfunction meaLeft(")[0]
        foot = mine.split("function meaFoot(")[1].split("\nfunction meaHeadText(")[0]
        self.assertEqual(body.count("meaFits("), 2, "the value of a cell and the zero mark of a cell")
        self.assertEqual(foot.count("meaFits("), 1, "the total of a column")

    def test_the_panel_asks_for_numbers_from_one_route_only(self):
        mine = self.page.split("// -- measures: a matrix")[1]
        self.assertEqual(set(re.findall(r'api\("(\w+)"', mine)), {"measures"})
        self.assertNotIn("api(\"crumb\"", mine.split("// -- the Size panel, for a chat")[0])
        self.assertNotRegex(mine, r"https?://", "no outside address")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_of_the_page_parses(self):
        body = self.page.split("<script>")[1].split("</script>")[0]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))


class MeasuresSortTest(unittest.TestCase):
    """The sort of the chat rows in the Measures panel (view.html, the block "the sort"): a click on the name of the measure puts the
    chats in the order of its number, largest first; a second click turns the order; a third gives the first order back; the sort is in
    the address (msort, morder). The functions that decide the order and the address are pure, so they run in node on the text of the
    page. The clicks, the camera, the mark and the order of a real store were driven in headless Chrome, on the fixture of the panel
    and on a copy of it in which a chat that is not the newest has the most requests (a sort that did nothing passes the fixture)."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "breadcrumbs", "view.html"), encoding="utf-8") as f:
            cls.page = f.read()
        if "function meaSortNext(" not in cls.page or "function meaSortUrl(" not in cls.page:
            raise AssertionError("view.html has no sort block (meaSortNext ... meaSortUrl)")
        cls.mine = cls.page.split("// -- measures: a matrix")[1]
        cls.pure = "function meaSortNext(" + cls.page.split("function meaSortNext(")[1].split("function meaSortUrl(")[0]

    def node(self, script):
        """Run the pure functions of the sort and then `script` in node; the script prints one line of JSON, which is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sort.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.pure + "\n" + script)
            r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return json.loads(r.stdout)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_click_goes_largest_first_then_smallest_first_then_the_first_order(self):
        got = self.node("""
          const none = {key: "", dir: "desc"}, one = meaSortNext(none, "api_calls"), two = meaSortNext(one, "api_calls");
          console.log(JSON.stringify({one, two, three: meaSortNext(two, "api_calls"), other: meaSortNext(two, "messages"), blank: meaSortNext(one, "")}));""")
        self.assertEqual(got["one"], {"key": "api_calls", "dir": "desc"})
        self.assertEqual(got["two"], {"key": "api_calls", "dir": "asc"})
        self.assertEqual(got["three"], {"key": "", "dir": "desc"})
        self.assertEqual(got["other"], {"key": "messages", "dir": "desc"}, "the name of another measure starts at largest first")
        self.assertEqual(got["blank"], {"key": "api_calls", "dir": "desc"}, "no measure is shown: there is nothing to sort by")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_chat_rows_go_by_their_number_and_a_row_that_is_not_a_chat_keeps_its_place(self):
        got = self.node("""
          const rows = new Map(["a", "b", "c", "d"].map(k => [k, {kind: "sess"}]).concat([["x", {kind: "span"}]]));
          const num = {a: 3, b: 10, c: null, d: 7, x: 99}, total = k => num[k], keys = ["a", "b", "c", "x", "d"];
          console.log(JSON.stringify({desc: meaSortRows(keys, rows, total, {key: "m", dir: "desc"}), asc: meaSortRows(keys, rows, total, {key: "m", dir: "asc"}),
            none: meaSortRows(keys, rows, total, {key: "", dir: "desc"}), keys}));""")
        self.assertEqual(got["desc"], ["b", "d", "a", "x", "c"], "the span x stays at its place (the fourth), though its number is the largest")
        self.assertEqual(got["asc"], ["a", "d", "b", "x", "c"])
        self.assertEqual(got["none"], ["a", "b", "c", "x", "d"])
        self.assertEqual(got["keys"], ["a", "b", "c", "x", "d"], "the list that came in is not changed")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_equal_numbers_keep_the_first_order_and_no_number_comes_last_in_both_directions(self):
        got = self.node("""
          const keys = ["p", "q", "r", "s", "t", "u"], rows = new Map(keys.map(k => [k, {kind: "sess"}]));
          const num = {p: 5, q: 0, r: null, s: 5, t: null, u: 9}, total = k => num[k];
          console.log(JSON.stringify({desc: meaSortRows(keys, rows, total, {key: "m", dir: "desc"}), asc: meaSortRows(keys, rows, total, {key: "m", dir: "asc"})}));""")
        self.assertEqual(got["desc"], ["u", "p", "s", "q", "r", "t"], "zero is a number; r and t have none and keep their order")
        self.assertEqual(got["asc"], ["q", "p", "s", "u", "r", "t"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_sort_is_one_pair_in_the_hash_and_the_rest_of_the_hash_stays(self):
        got = self.node("""
          const desc = {key: "api_calls", dir: "desc"}, asc = {key: "api_calls", dir: "asc"}, none = {key: "", dir: "desc"}, base = "#k=abc123&measures=1&mopen=s%3Atoday&mincl=0";
          console.log(JSON.stringify({desc: meaSortHash(base, desc), asc: meaSortHash(base, asc), again: meaSortHash(meaSortHash(base, asc), desc),
            gone: meaSortHash(meaSortHash(base, asc), none), bare: meaSortHash("#k=abc123", desc), off: meaSortHash("#k=abc123&measures=0", desc), empty: meaSortHash("", none)}));""")
        base = "#k=abc123&measures=1&mopen=s%3Atoday&mincl=0"
        self.assertEqual(got["desc"], base + "&msort=api_calls")
        self.assertEqual(got["asc"], base + "&msort=api_calls&morder=asc")
        self.assertEqual(got["again"], base + "&msort=api_calls", "a new sort takes the old pair out")
        self.assertEqual(got["gone"], base, "the first order takes the pair out and leaves the rest as it was")
        self.assertEqual(got["bare"], "#k=abc123&measures=1&msort=api_calls", "a sort puts the panel on in the link, so that the link opens it")
        self.assertEqual(got["off"], "#k=abc123&measures=1&msort=api_calls")
        self.assertEqual(got["empty"], "")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_a_sort_belongs_to_the_measure_that_is_shown(self):
        got = self.node("""
          const MEA = {sort: {key: "api_calls", dir: "asc"}, measure: "api_calls"}, same = meaSortNow();
          MEA.measure = "messages";
          console.log(JSON.stringify({same, other: meaSortNow(), unset: (MEA.sort = {key: "", dir: "desc"}, meaSortNow())}));""")
        self.assertEqual(got["same"], {"key": "api_calls", "dir": "asc"})
        self.assertEqual(got["other"], {"key": "", "dir": "desc"}, "another measure is shown: the first order")
        self.assertEqual(got["unset"], {"key": "", "dir": "desc"})

    def test_the_name_of_the_measure_is_the_click_target_and_the_page_reads_the_link(self):
        hit = self.mine.split("function meaHitTest(")[1].split("\nfunction meaZoom(")[0]
        self.assertIn('{area: "sort"}', hit)
        self.assertLess(hit.index('{area: "sort"}'), hit.index('{area: "head", col}'), "the name is tested before the columns of the head")
        self.assertIn("meaSortClick()", self.mine.split("function meaClick(")[1].split("\nfunction meaSetUnit(")[0])
        self.assertIn("meaSortName(", self.mine.split("function meaHead(")[1].split("\n}\n")[0], "the head draws the name and its mark")
        init = self.mine.split("(function meaInit()")[1].split("})();")[0]
        self.assertIn('h("msort")', init)
        self.assertIn('h("morder") === "asc"', init)
        doc = self.mine.split("// The URL takes #measures=1")[1].split("const MEAS =")[0]
        self.assertTrue("msort=" in doc and "morder=asc" in doc, "the list of the link parts names the sort")

    def test_the_order_is_the_total_in_the_right_margin_and_a_new_answer_keeps_the_sort_of_the_measure_that_is_shown(self):
        prepare = self.mine.split("function meaPrepare(")[1].split("\n}\n")[0]
        self.assertIn("meaSortRows(keys, MEA.rows, k => meaRowTotal(k).sum, sort)", prepare, "the number that the margin shows")
        self.assertIn("meaRowTotal(r.key)", self.mine.split("function meaRight(")[1].split("\n}\n")[0])
        load = self.mine.split("async function meaLoad(")[1].split("\n}\n")[0]
        self.assertLess(load.index("meaChoose(d.dims)"), load.index("meaSortKeep();"), "the measure that is shown is known first")
        self.assertLess(load.index("meaSortKeep();"), load.index("meaMerge(next, d, true)"))
        choose = self.mine.split("function meaChoose(")[1].split("\n}\n")[0]
        self.assertIn("MEA.sort.key", choose, "a link that has msort and no mmeasure shows the measure of the sort")

    def test_a_sort_of_a_capped_list_says_that_it_orders_only_the_chats_that_the_panel_has(self):
        info = self.mine.split("function meaInfo(")[1].split("\n}\n")[0]
        self.assertIn("and the sort orders these only", info)
        self.assertIn('(meaSortNow().key ? ", and the sort orders these only. " : ". ")', info, "the words come only while a sort is on")
        self.assertIn("meaInfo();", self.mine.split("function meaSortSet(")[1].split("\n}\n")[0], "a click on the name refreshes the line")

    def test_the_address_follows_the_sort_with_no_new_entry_in_the_history_and_nothing_else_is_stored(self):
        self.assertEqual(self.page.count("history.replaceState("), 1)
        self.assertNotIn("pushState", self.page)
        self.assertNotIn("localStorage.setItem(\"breadcrumbs.sort", self.page)
        url = self.mine.split("function meaSortUrl(")[1].split("\n}\n")[0]
        self.assertIn("location.pathname + location.search + meaSortHash(location.hash, meaSortNow())", url, "the path and the query are kept")


def local(day, hh=0, mm=0, month=11):
    """A local clock time of 2026 in seconds since the epoch: the day planner cuts the days at the local midnight."""
    return time.mktime((2026, month, day, hh, mm, 0, 0, 0, -1))


# sid: (parent, how, started, ended, last activity). Three days, a chat that overlaps another, a chat over midnight, a chat with
# no end, a family: a lead, two helpers that it started while it ran, and a continuation three minutes long.
CAL_DAYS = {
    "tea1": (None, "cli", local(3, 9, 0), local(3, 10, 30), None),
    "tea2": (None, "cli", local(3, 10, 0), local(3, 11, 0), None),
    "bike": (None, "cli", local(3, 23, 30), local(4, 1, 15), None),
    "walk": (None, "telegram", local(4, 14, 0), None, local(4, 15, 20)),
    "lead": (None, "cli", local(5, 9, 0), local(5, 12, 0), None),
    "help1": ("lead", "subagent", local(5, 9, 30), local(5, 10, 15), None),
    "help2": ("lead", "subagent", local(5, 10, 0), local(5, 11, 0), None),
    "cont": ("lead", "cli", local(5, 12, 30), local(5, 12, 33), None),
}


class CalendarData(ViewData):
    """The store of ViewData (tea1, tea2, bike) plus the other chats of CAL_DAYS, and beside it a made-up state.db that times them."""

    def setUp(self):
        super().setUp()
        for sid in CAL_DAYS:
            if sid not in self.sess:
                self.p.sync_turn("a question in %s about the kettle" % sid, "an answer in %s about the water" % sid, session_id=sid)
                self.sess[sid] = self.p.session_crumb(sid)
        self.db = os.path.join(self.tmp.name, "state.db")
        state_db(self.db, [{"id": sid, "parent": par, "how": how, "t0": a, "t1": b, "last": last}
                           for sid, (par, how, a, b, last) in CAL_DAYS.items()], [])


class CalendarApiTest(CalendarData):
    """View.calendar and /api/calendar: when each session of the map ran, from a few columns of state.db or a few crumbs."""

    def test_each_chat_has_the_times_the_parent_and_the_channel_of_its_state_db_row(self):
        got = {s["sid"]: s for s in self.open().calendar()["sessions"]}
        self.assertEqual(set(got), set(CAL_DAYS))
        for sid, (par, how, a, b, last) in CAL_DAYS.items():
            s = got[sid]
            self.assertEqual((s["id"], s["t0"], s["t1"], s["parent"], s["how"]), (self.sess[sid], a, b or last, par, how), sid)
            if sid not in SESSIONS:  # the chats that this test made: the first line of the first question, as the crumb titles it
                self.assertEqual(s["title"], "user: a question in %s about the kettle" % sid)
            self.assertTrue(s["title"].startswith("user: "), sid)

    def test_a_chat_with_no_end_runs_to_its_last_activity(self):
        walk = next(s for s in self.open().calendar()["sessions"] if s["sid"] == "walk")
        self.assertEqual((walk["t1"] - walk["t0"]) / 60, 80)

    def test_a_chat_that_state_db_lacks_takes_the_time_of_its_first_and_last_turn(self):
        self.p.clock = lambda: local(6, 8, 0)
        self.p.sync_turn("an early question", "an early reply", session_id="solo")
        self.p.clock = lambda: local(6, 8, 45)
        self.p.sync_turn("a late question", "a late reply", session_id="solo")
        ans = self.open().calendar()
        solo = next(s for s in ans["sessions"] if s["sid"] == "solo")
        self.assertEqual((solo["t0"], solo["t1"], solo["parent"], solo["how"]), (local(6, 8, 0), local(6, 8, 45), None, None))
        self.assertTrue(ans["has_db"])

    def test_without_a_state_db_every_time_is_a_turn_time_and_no_chat_has_a_parent(self):
        os.remove(self.db)
        ans = self.open().calendar()
        self.assertEqual((ans["has_db"], len(ans["sessions"])), (False, len(CAL_DAYS)))
        for s in ans["sessions"]:
            self.assertTrue(0 < s["t0"] <= s["t1"] and abs(s["t0"] - time.time()) < 3600, s)
            self.assertEqual((s["parent"], s["how"]), (None, None))

    def test_an_end_before_the_start_is_cut_to_the_start(self):
        db = sqlite3.connect(self.db)  # the test's own made-up file
        db.execute("UPDATE sessions SET ended_at = ?, last_activity_at = NULL WHERE id = 'tea1'", (local(3, 8, 0),))
        db.commit()
        db.close()
        tea = next(s for s in self.open().calendar()["sessions"] if s["sid"] == "tea1")
        self.assertEqual((tea["t0"], tea["t1"]), (local(3, 9, 0), local(3, 9, 0)))

    def test_the_answer_lists_the_sessions_of_the_graph_newest_start_first_and_caps(self):
        v = self.open()
        ans = v.calendar()
        self.assertEqual({s["id"] for s in ans["sessions"]}, {n["id"] for n in v.graph()["nodes"]})
        t0s = [s["t0"] for s in ans["sessions"]]
        self.assertEqual(t0s, sorted(t0s, reverse=True))
        self.assertEqual((ans["total"], ans["capped"]), (len(CAL_DAYS), False))
        small = v.calendar(limit=3)
        self.assertEqual((len(small["sessions"]), small["total"], small["capped"]), (3, len(CAL_DAYS), True))

    def test_the_answer_reads_a_few_crumbs_of_each_chat_not_its_tree(self):
        def reads(turns):
            for i in range(turns):
                self.p.sync_turn("question %d about the kettle" % i, "answer %d about the tea" % i, session_id="long")
            v = self.open()
            seen, get = [], v.store.get
            v.store.get = lambda cid, *a, **k: (seen.append(cid), get(cid, *a, **k))[1]
            v.calendar()
            return len(seen)

        few = reads(2)
        many = reads(60) - few  # 60 turns: more than 16 kids, so spans between the chat and its turns
        self.assertLessEqual(many, 6, "a chat of 60 turns costs a span or two more, never a read of each turn")

    def test_only_the_calendar_columns_of_the_sessions_table_are_read_and_no_text_is_in_the_answer(self):
        reads = set()

        def connect(*a, **k):
            conn = sqlite3.connect(*a, **k)
            conn.set_authorizer(lambda action, t, c, db, src: (reads.add((t, c)) if action == sqlite3.SQLITE_READ else None)
                                or sqlite3.SQLITE_OK)
            return conn

        v = self.open()
        v.db_connect = connect
        ans = v.calendar()
        cols = {(t, c) for t, c in reads if not t.startswith("sqlite_")}
        self.assertEqual({t for t, c in cols}, {"sessions"}, "never the messages table")
        self.assertEqual({c for t, c in cols}, set(view.CAL_COLS))
        self.assertNotIn(SECRET, json.dumps(ans))

    def test_state_db_is_not_changed_and_gets_no_journal(self):
        before = (os.stat(self.db).st_size, os.stat(self.db).st_mtime_ns, sorted(os.listdir(self.tmp.name)))
        self.open().calendar()
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        urllib.request.urlopen("http://127.0.0.1:%d/api/calendar?k=%s" % (server.server_address[1], view.token(self.root))).read()
        self.assertEqual((os.stat(self.db).st_size, os.stat(self.db).st_mtime_ns, sorted(os.listdir(self.tmp.name))), before)

    def test_the_route_needs_the_token_and_answers_errors_in_the_api_way(self):
        server = view.make_server(self.root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:%d/api/calendar" % server.server_address[1]

        def code(url):
            try:
                with urllib.request.urlopen(url) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                e.close()
                return e.code, None

        k = "?k=" + view.token(self.root)
        self.assertEqual(code(base)[0], 403)
        status, d = code(base + k)
        self.assertEqual((status, sorted(d), len(d["sessions"])), (200, ["capped", "has_db", "sessions", "total"], len(CAL_DAYS)))
        self.assertEqual(sorted(d["sessions"][0]), ["how", "id", "parent", "sid", "t0", "t1", "title"])
        self.assertEqual(len(code(base + k + "&limit=2")[1]["sessions"]), 2)
        self.assertEqual(code(base + k + "&limit=many")[0], 400)


# The part of view.html between "@cal-plan begin" and "@cal-plan end" reads no page state, so node can run it (no browser).
CAL_JS = r"""
const vm = require('vm'), job = JSON.parse(require('fs').readFileSync(0, 'utf8')), ctx = vm.createContext({});
vm.runInContext(job.src + '\nglobalThis.cal = {calPlan, calApply, CALGEO};', ctx);
const out = {plan: ctx.cal.calPlan(job.items, job.opts || {})};
if (job.roots) { out.roots = job.roots.map(k => ({key: k})); ctx.cal.calApply(out.plan, out.roots); }
process.stdout.write(JSON.stringify(out));
"""


def cal_plan(items, roots=None, opts=None, tz=None):
    """calPlan (and calApply, with roots) of breadcrumbs/view.html run in node. -> {plan, roots}. tz: the zone of the run (TZ)."""
    with open(view.PAGE, encoding="utf-8") as f:
        src = f.read().split("// @cal-plan begin")[1].split("// @cal-plan end")[0]
    env = dict(os.environ, **({"TZ": tz} if tz else {}))
    r = subprocess.run(["node", "-e", CAL_JS], input=json.dumps({"src": src, "items": items, "roots": roots, "opts": opts}),
                       capture_output=True, text=True, timeout=60, env=env)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CalendarLayoutTest(CalendarData):
    """The calendar layout (a day planner) of the map. The fixture is CAL_DAYS: three days, an overlap, a chat over midnight, a
    chat with no end and a family. Every expected number is made here from the times of CAL_DAYS, with Python's clock and view._midnight."""

    def setUp(self):
        super().setUp()
        sessions = self.open().calendar()["sessions"]
        self.items = [{"key": s["id"], "t0": s["t0"], "t1": s["t1"], "parent": next((x["id"] for x in sessions if x["sid"] == s["parent"]), None)}
                      for s in sessions]
        self.key = {sid: ref for sid, ref in self.sess.items() if sid in CAL_DAYS}
        self.out = cal_plan(self.items, roots=[i["key"] for i in self.items])
        self.plan = self.out["plan"]
        self.hour = self.plan["geo"]["hour"]
        self.pieces = {}
        for p in self.plan["pieces"]:
            self.pieces.setdefault(p["key"], []).append(p)
        for ps in self.pieces.values():
            ps.sort(key=lambda p: p["t0"])

    def piece(self, sid, n=0):
        return self.pieces[self.key[sid]][n]

    def test_the_x_of_a_block_is_its_start_and_its_width_is_its_length(self):
        for sid, (par, how, a, b, last) in CAL_DAYS.items():
            end = b or last
            if sid == "bike":
                continue  # over midnight: see below
            p = self.piece(sid)
            self.assertEqual(len(self.pieces[self.key[sid]]), 1, sid)
            self.assertAlmostEqual(p["x0"], (a - view._midnight(a)) / 3600 * self.hour, places=6, msg=sid)
            if end - a >= 600:
                self.assertAlmostEqual(p["x1"] - p["x0"], (end - a) / 3600 * self.hour, places=6, msg=sid)
                self.assertFalse(p["short"], sid)
        self.assertEqual(self.piece("walk")["x1"] - self.piece("walk")["x0"], self.hour * 80 / 60)  # 80 minutes, from the last activity
        self.assertEqual(self.plan["geo"]["hour"] * 24, 2880)

    def test_a_session_shorter_than_ten_minutes_is_drawn_ten_minutes_wide(self):
        p = self.piece("cont")  # three minutes
        self.assertTrue(p["short"])
        self.assertAlmostEqual(p["x1"] - p["x0"], 10 / 60 * self.hour, places=6)
        self.assertAlmostEqual(p["x0"], 12.5 * self.hour, places=6)  # it still starts at its start

    def test_no_two_blocks_of_one_sub_lane_overlap_and_chats_that_overlap_take_two_lanes(self):
        lanes = {}
        for p in self.plan["pieces"]:
            lanes.setdefault((p["band"], p["lane"]), []).append(p)
        for row in lanes.values():
            row.sort(key=lambda p: p["x0"])
            for left, right in zip(row, row[1:]):
                self.assertGreaterEqual(right["x0"] - left["x1"], self.plan["geo"]["gap"])
        a, b = self.piece("tea1"), self.piece("tea2")  # 09:00 to 10:30 and 10:00 to 11:00
        self.assertEqual(a["band"], b["band"])
        self.assertNotEqual(a["lane"], b["lane"])
        self.assertLess(b["x0"], a["x1"])
        for p in self.plan["pieces"]:  # a lane has a height, and the band holds its lanes
            band = self.plan["bands"][p["band"]]
            self.assertAlmostEqual(p["y1"] - p["y0"], self.plan["geo"]["lane"])
            self.assertTrue(band["y"] + self.plan["geo"]["head"] <= p["y0"] and p["y1"] <= band["y"] + band["h"])

    def test_a_session_over_midnight_is_two_pieces_of_one_session(self):
        first, second = self.pieces[self.key["bike"]]
        a, b = CAL_DAYS["bike"][2], CAL_DAYS["bike"][3]
        day, nxt = view._midnight(a), view._midnight(a, 1)
        bands = self.plan["bands"]
        self.assertEqual((bands[first["band"]]["day"], bands[second["band"]]["day"]), (day, nxt))
        self.assertEqual(second["band"] + 1, first["band"], "the newest day is on top: the next day is the band above")
        self.assertEqual((first["cutR"], first["cutL"], second["cutL"], second["cutR"]), (True, False, True, False))
        self.assertAlmostEqual(first["x0"], 23.5 * self.hour)
        self.assertAlmostEqual(first["x1"], bands[first["band"]]["w"])  # to 24:00
        self.assertAlmostEqual(second["x0"], 0)  # from 00:00
        self.assertAlmostEqual(second["x1"], 1.25 * self.hour)  # to 01:15
        self.assertAlmostEqual((first["x1"] - first["x0"]) + (second["x1"] - second["x0"]), (b - a) / 3600 * self.hour)
        self.assertEqual(len([p for p in self.plan["pieces"] if p["key"] == self.key["bike"]]), 2)

    def test_there_is_a_band_for_each_day_newest_on_top_with_the_date_and_the_hours_of_its_clock(self):
        days = [view._midnight(local(d)) for d in (5, 4, 3)]
        bands = self.plan["bands"]
        self.assertEqual([b["day"] for b in bands], days)
        self.assertEqual([b["y"] for b in bands], sorted(b["y"] for b in bands))
        for b in bands:
            self.assertEqual((b["w"], b["clock"][:3], b["clock"][-1]), (2880, [0, 1, 2], 0))
            self.assertEqual(b["end"], view._midnight(b["day"], 1))
        self.assertEqual(self.plan["w"], 2880)
        self.assertEqual(bands[0]["y"], 0)
        self.assertAlmostEqual(self.plan["h"], bands[-1]["y"] + bands[-1]["h"])

    def test_a_child_sits_under_its_parent_and_each_family_line_ends_at_its_child(self):
        lead = self.piece("lead")
        self.assertEqual(self.piece("help1")["lane"], lead["lane"] + 1)
        self.assertEqual(self.piece("help2")["lane"], lead["lane"] + 2, "the first helper holds the lane under the lead while the second one runs")
        self.assertEqual(self.piece("cont")["lane"], lead["lane"] + 1, "the continuation starts after the lead: the lane under it is free")
        lines = {l["to"]: l for l in self.plan["lines"]}
        self.assertEqual(set(lines), {self.key[s] for s in ("help1", "help2", "cont")})
        for sid in ("help1", "help2", "cont"):
            l, child = lines[self.key[sid]], self.piece(sid)
            self.assertEqual(l["from"], self.key["lead"])
            on_top = abs(l["by"] - child["y0"]) < 1e-9 and child["x0"] <= l["bx"] <= child["x1"]
            on_left = abs(l["bx"] - child["x0"]) < 1e-9 and child["y0"] <= l["by"] <= child["y1"]
            self.assertTrue(on_top or on_left, (sid, l, child))  # the line ends on the child's block
            on_bottom = abs(l["ay"] - lead["y1"]) < 1e-9 and lead["x0"] <= l["ax"] <= lead["x1"]
            on_right = abs(l["ax"] - lead["x1"]) < 1e-9 and lead["y0"] <= l["ay"] <= lead["y1"]
            self.assertTrue(on_bottom or on_right, (sid, l, lead))  # and starts on the parent's
        self.assertEqual([lines[self.key[s]]["kind"] for s in ("help1", "help2", "cont")], ["drop", "drop", "flow"])
        self.assertEqual(len(self.plan["lines"]), 3, "the other chats have no parent")

    def test_the_box_of_a_session_is_its_first_block(self):
        roots = {r["key"]: r for r in self.out["roots"]}
        ox, oy = self.plan["ox"], self.plan["oy"]
        self.assertEqual((ox, oy), (-self.plan["w"] / 2, -self.plan["h"] / 2))
        for sid in CAL_DAYS:
            r, p = roots[self.key[sid]], self.piece(sid)
            self.assertAlmostEqual(r["tx"] - r["tw"] / 2, ox + p["x0"])
            self.assertAlmostEqual(r["tx"] + r["tw"] / 2, ox + p["x1"])
            self.assertAlmostEqual(r["ty"] - r["th"] / 2, oy + p["y0"])
            self.assertAlmostEqual(r["ty"] + r["th"] / 2, oy + p["y1"])
        bike = roots[self.key["bike"]]
        self.assertEqual([(p["cutL"], p["cutR"]) for p in bike["cal"]], [(False, True), (True, False)], "its blocks, the first first")

    def test_an_empty_day_is_a_thin_band_with_its_date_and_a_long_run_of_them_is_one_band(self):
        items = [{"key": "a", "t0": local(1, 10), "t1": local(1, 11)}, {"key": "b", "t0": local(4, 10), "t1": local(4, 11)}]
        plan = cal_plan(items)["plan"]
        geo = plan["geo"]
        one = geo["head"] + geo["lane"] + geo["gap"] + 2  # a band with one lane
        self.assertEqual([(view._midnight(local(d)) == b["day"], b["empty"], b["h"]) for d, b in zip((4, 3, 2, 1), plan["bands"])],
                         [(True, False, one), (True, True, geo["thin"]), (True, True, geo["thin"]), (True, False, one)])
        far = cal_plan([{"key": "a", "t0": local(1, 10), "t1": local(1, 11)}, {"key": "b", "t0": local(22, 10), "t1": local(22, 11)}])["plan"]["bands"]
        self.assertEqual(len(far), 3)
        self.assertEqual((far[1]["empty"], far[1]["gap"], far[1]["h"]), (True, 20, geo["thin"]), "the 20 days between the two are one thin band")

    def test_a_session_with_no_time_is_not_lost(self):
        plan = cal_plan([{"key": "a", "t0": local(2, 10), "t1": local(2, 11)}, {"key": "b", "t0": None, "t1": None},
                         {"key": "c", "t0": None, "t1": None}], roots=["a", "b", "c"])
        last = plan["plan"]["bands"][-1]
        self.assertEqual((last["undated"], last["day"]), (2, None))
        self.assertEqual(sorted(p["key"] for p in plan["plan"]["pieces"] if p.get("undated")), ["b", "c"])
        self.assertTrue(all("tx" in r for r in plan["roots"]))

    def test_a_chat_with_a_parent_that_the_map_lacks_has_no_line_and_a_cycle_does_no_harm(self):
        items = [{"key": "a", "t0": local(2, 10), "t1": local(2, 11), "parent": "gone"},
                 {"key": "b", "t0": local(2, 10, 30), "t1": local(2, 12), "parent": "c"},
                 {"key": "c", "t0": local(2, 10, 40), "t1": local(2, 12), "parent": "b"},
                 {"key": "d", "t0": local(2, 13), "t1": local(2, 14), "parent": "d"}]
        plan = cal_plan(items)["plan"]
        self.assertEqual(sorted((l["from"], l["to"]) for l in plan["lines"]), [("b", "c"), ("c", "b")])
        self.assertEqual(len(plan["pieces"]), 4)

    def test_a_day_of_25_hours_and_a_day_of_23_hours_keep_the_clock_of_their_hour_marks(self):
        fall = cal_plan([{"key": "a", "t0": calendar.timegm((2026, 11, 1, 5, 30, 0)), "t1": calendar.timegm((2026, 11, 1, 9, 0, 0))}],
                        tz="America/Chicago")["plan"]["bands"][0]  # Sun Nov 1 2026: the clocks go back at 02:00
        if fall["w"] != 3000:
            self.skipTest("this node has no time zone data for America/Chicago")
        self.assertEqual((fall["w"], len(fall["clock"])), (3000, 26))
        self.assertEqual(fall["clock"][:4], [0, 1, 1, 2], "the hour from 01:00 to 02:00 comes twice")
        spring = cal_plan([{"key": "a", "t0": calendar.timegm((2026, 3, 8, 6, 30, 0)), "t1": calendar.timegm((2026, 3, 8, 9, 0, 0))}],
                          tz="America/Chicago")["plan"]["bands"][0]  # Sun Mar 8 2026: the clocks go forward at 02:00
        self.assertEqual((spring["w"], spring["clock"][:4]), (2760, [0, 1, 3, 4]))


class CalendarPageTest(unittest.TestCase):
    """breadcrumbs/view.html: the calendar layout. The rest of the page is the Measures test's and the layouts' own."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "breadcrumbs", "view.html"), encoding="utf-8") as f:
            cls.page = f.read()
        cls.cal = cls.page.split("// -- the calendar layout: a day planner")[1].split("// -- the camera and the zoom stack")[0]

    def test_the_layout_has_a_button_and_a_hash_like_the_others(self):
        layouts = re.search(r"const LAYOUT = \{(.*?)\n\};", self.page, re.S).group(1)
        self.assertEqual(re.findall(r"^  (\w+): \[", layouts, re.M), ["grid", "treemap", "tree", "radial", "force", "calendar"])
        self.assertIn('calendar: ["Calendar", ', layouts)
        self.assertIn('HASH.get("layout") || ARG.get("layout")', self.page)  # #layout=calendar goes through setLayout, which takes any key of LAYOUT
        self.assertIn('layout = k in LAYOUT ? k : "grid"', self.page)

    def test_the_calendar_asks_for_its_times_from_one_route_and_never_reads_a_crumb_tree(self):
        mine = self.cal + self.page.split("// -- the calendar: drawing")[1].split("function draw(t)")[0]
        self.assertEqual(set(re.findall(r'api\("([\w/]+)"', mine)), {"calendar"})  # not /api/measures/labels either: the cue words of a chat cost a read of its text
        self.assertNotIn("cueLabel", self.page)
        for word in ("crumb(", "doc(", "parts(", "buildDoc"):
            self.assertNotIn(word, self.cal, word)
        self.assertNotRegex(mine, r"https?://", "no outside address")

    def test_the_drawing_reads_no_plan_while_the_times_are_on_their_way(self):
        # the grid stands in until /api/calendar answers: CAL.plan is null, and a draw that read it would throw in every frame
        for name in ("calUnder", "calOver"):
            body = self.page.split("function %s() {" % name)[1].split("\n}\n")[0]
            self.assertIn("if (!CAL.plan) return;", body, name)
        self.assertIn("if (!P || !pcs || !pcs.length)", self.page.split("function drawCal(n, t) {")[1].split("\n}\n")[0])
        self.assertIn('if (layout === "calendar") calUnder();', self.page)
        self.assertIn('if (layout === "calendar") calOver();', self.page)

    def test_a_head_that_stuck_to_the_top_is_drawn_over_the_blocks_on_a_strip(self):
        heads = self.page.split("function calHeads(over) {")[1].split("\n}\n")[0]
        self.assertIn("if ((!b.empty && mid < top + fs) !== over) return;", heads)  # each head is drawn once: under the blocks, or over them when it stuck
        self.assertIn('if (over) { g.fillStyle = rgba(css("bg"), 0.92); g.fillRect(', heads)
        self.assertIn("calHeads(false);", self.page.split("function calUnder() {")[1].split("\n}\n")[0])
        self.assertIn("calHeads(true);", self.page.split("function calOver() {")[1].split("\n}\n")[0])

    def test_a_page_that_starts_in_the_calendar_does_not_ask_for_the_graph_and_the_graph_fills_the_quick_boxes_in(self):
        load = self.page.split("function load() {")[1].split("async function reveal(")[0]
        self.assertIn('if (layout === "calendar") calLoad(); else loadGraph();', load)
        self.assertIn("if (n && n.quick) Object.assign(n, info, {quick: false", load)
        self.assertEqual(self.page.count('api("graph")'), 1, "the map's first answer is asked in one place, loadGraph")
        self.assertIn("else if (GRAPH.started) loadGraph();", self.page.split("function arrange() {")[0], "a layout that needs the cue words asks for the graph")
        self.assertIn("const open = Math.min(pw, ph) >= OPEN && (!n.quick ||", self.page, "a box with no token count never picks a text level")

    def test_the_camera_and_the_other_layouts_keep_their_code(self):
        self.assertEqual(self.page.count("function damp("), 1)
        self.assertEqual(self.page.count("function spring("), 1)
        self.assertEqual(self.page.count("function camStep("), 1)
        self.assertEqual(self.page.count("const MOVE = {"), 1)
        arrange = self.page.split("function arrange() {")[1].split("\nfunction place(")[0]
        for line in ('if (layout === "force" || !roots.length) return;', 'else if (layout === "treemap") {',
                     "} else pack(forest().map(t => (layout === \"tree\" ? treeAt : ringsAt)(t, c)), aspect);"):
            self.assertIn(line, arrange)
        self.assertEqual(arrange.count("calArrange()"), 1, "the calendar has one clause in arrange(); the other layouts do not know it")
        self.assertEqual(self.page.split("function step() {")[1].split("\n}\n")[0].count("calendar"), 0, "the force model is not touched")


class ApiTest(unittest.TestCase):
    def test_the_api_table_keeps_its_routes(self):
        """hermes-spacegraph's routes call view.API, so its names are a public contract (a new route may join)."""
        self.assertEqual(sorted(view.API), ["/api/ace", "/api/context", "/api/crumb", "/api/find", "/api/graph",
                                            "/api/path", "/api/session", "/api/trail", "/api/venn"])
        # the explorer's own routes (the Measures panel, the calendar layout): the dashboard table (tests/test_dashboard.py) is not changed by them
        self.assertEqual(sorted(view.EXPLORER_API), ["/api/calendar", "/api/measures", "/api/measures/labels"])

if __name__ == "__main__":
    unittest.main()
