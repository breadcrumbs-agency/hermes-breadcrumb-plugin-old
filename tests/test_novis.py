"""no-vis (breadcrumbs/novis.py): the private command, and the views that leave out what it tags."""
import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # the scenario never starts the real model
os.environ.setdefault("SPACEGRAPH_NO_WARM", "1")  # loading the router must not warm a real store

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from breadcrumbs import cli, island, novis, viz  # noqa: E402
from breadcrumbs.view import View  # noqa: E402
from test_context import scenario  # noqa: E402
from test_dashboard import FASTAPI, PREFIX, plugin_client  # noqa: E402


def state_db(home, rows):
    """A state.db with the columns that novis reads: (id, started_at, title)."""
    os.makedirs(home, exist_ok=True)
    con = sqlite3.connect(os.path.join(home, "state.db"))
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, title TEXT)")
    con.executemany("INSERT INTO sessions VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


class Root(unittest.TestCase):
    """A scratch Hermes root: the default profile and alpha, each with a state.db, and a session only in alpha's store."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="bc-novis-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.alpha = os.path.join(self.root, "profiles", "alpha")
        state_db(self.root, [("s-default", 100.0, "a chat of the default profile")])
        state_db(self.alpha, [("s-old", 200.0, "Bike   chain notes"), ("s-new", 300.0, "garden")])
        os.makedirs(os.path.join(self.alpha, "breadcrumbs", "refs"))
        Path(self.alpha, "breadcrumbs", "refs", "sess-imported").write_text("")

    def run_(self, action, *names):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = novis._command(action, list(names), hermes=self.root, out=out)
        return rc, out.getvalue(), err.getvalue()


class Command(Root):
    def test_add_list_rm(self):
        rc, out, _ = self.run_("add", "alpha", "s-old", "imported")
        self.assertEqual(rc, 0)
        self.assertEqual(novis.load(self.root), {"profiles": ["alpha"], "sessions": ["imported", "s-old"]})
        self.assertEqual(self.run_("list")[1], out)
        self.assertIn("[x] profile  alpha\n", out)
        self.assertRegex(out, r"\[x\] session  s-old  alpha  \S+ \S+  Bike chain notes\n")  # its profile, start, title
        self.assertIn("[x] session  imported\n", out)  # in a store only: no state.db row
        rc, _, err = self.run_("rm", "alpha", "s-nope")
        self.assertEqual((rc, novis.load(self.root)["profiles"]), (1, ["alpha"]))  # nothing changed
        self.assertIn("s-nope not tagged", err)
        self.assertEqual(self.run_("rm", "alpha", "imported", "s-old")[0], 0)
        self.assertEqual(novis.load(self.root), {"profiles": [], "sessions": []})
        self.assertIn("nothing is tagged", self.run_("list")[1])

    def test_add_refuses_a_name_that_is_no_profile_and_no_session(self):
        rc, _, err = self.run_("add", "s-new", "nobody", "../alpha")
        self.assertEqual(rc, 1)
        self.assertIn("no profile or session nobody, ../alpha", err)
        self.assertFalse(os.path.exists(novis.path(self.root)))

    def test_find_matches_names_ids_and_titles_and_shows_the_tag(self):
        self.run_("add", "s-old")
        out = self.run_("find", "CHAIN")[1]
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("[x] session  s-old  alpha"))
        out = self.run_("find")[1].splitlines()  # no TEXT: every profile, then the sessions, newest first
        self.assertEqual([line[:20] for line in out], ["[ ] profile  default", "[ ] profile  alpha", "[ ] session  s-new  ",
                                                       "[x] session  s-old  ", "[ ] session  s-defau"])

    def test_a_file_that_does_not_parse_is_reported_and_hides_everything(self):
        os.makedirs(os.path.dirname(novis.path(self.root)), exist_ok=True)
        Path(novis.path(self.root)).write_text("{not json")
        rc, _, err = self.run_("add", "s-new")
        self.assertEqual(rc, 1)
        self.assertIn("fix the file or delete it", err)
        hide = novis.Hide(self.alpha, self.root)
        self.assertTrue(hide("s-new") and hide(""))
        self.assertIn("cannot be read", hide.why())

    def test_hide_follows_the_file(self):
        hide, default = novis.Hide(self.alpha, self.root), novis.Hide(self.root, self.root)
        self.assertFalse(hide("s-old"))
        self.run_("add", "s-old")
        self.assertEqual([hide("s-old"), hide("s-new"), hide(""), hide("s-ol", True)], [True, False, False, True])
        self.assertEqual(hide.why("s-old"), "this chat opted out of visualization (no-vis)")
        self.run_("add", "alpha")
        self.assertTrue(hide("s-new") and hide(""))
        self.assertEqual(hide.why(), "profile `alpha` opted out of visualization (no-vis)")
        self.assertEqual((default("s-new"), default.why()), (False, ""))
        self.assertEqual((novis.profile_at(self.root, self.root), novis.profile_at("/elsewhere", self.root)),
                         ("default", None))


class Chats(Root):
    """A chat of a tagged profile or session: no explorer at start, no NOTE, and /viz opens nothing."""

    def setUp(self):
        super().setUp()
        self.run_("add", "alpha", "s-new")

    def test_no_explorer_and_no_note(self):
        with mock.patch.object(island, "_viz_started", False), \
                mock.patch.object(island.threading, "Thread") as thread, \
                mock.patch.object(island, "launch_root", return_value=None):
            island.Tracker(hermes=self.root, profile_home=self.alpha).on_session_start("s-free", platform="cli")
            island.Tracker(hermes=self.root, profile_home=self.root).on_session_start("s-new", platform="cli")
            self.assertEqual(thread.call_count, 0)
            island.Tracker(hermes=self.root, profile_home=self.root).on_session_start("s-free", platform="cli")
            self.assertEqual(thread.call_count, 1)  # an untagged chat of an untagged profile
        with mock.patch.object(viz, "run") as run:
            self.assertEqual(island.open_viz(self.alpha, hermes=self.root), "breadcrumbs: the explorer did not open: "
                             "profile `alpha` opted out of visualization (no-vis).")
            self.assertIn("this chat opted out", island.open_viz(self.root, hermes=self.root, session_id="s-new"))
        run.assert_not_called()
        with mock.patch.object(island, "own_root", return_value="/w/a"), \
                mock.patch.object(island, "launch_root", return_value="/w/a"):  # launched inside the island
            t = island.Tracker(hermes=self.root, profile_home=self.root)
            self.assertIsNone(t.transform_llm_output("hi", session_id="s-new"))
            self.assertIn("[!NOTE]", t.transform_llm_output("hi", session_id="s-free") or "")

    def test_the_commands_that_draw_refuse_a_tagged_profile_and_the_private_command_is_not_in_help(self):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(island, "hermes_root", return_value=self.root), \
                mock.patch("breadcrumbs.view.make_server") as make, contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--home", os.path.join(self.alpha, "breadcrumbs"), "serve"]), 1)
        make.assert_not_called()
        self.assertIn("profile `alpha` opted out of visualization", err.getvalue())
        with contextlib.redirect_stdout(out):
            cli.main(["help"])
        self.assertNotIn("no-vis", out.getvalue())
        out = io.StringIO()
        with mock.patch.object(island, "hermes_root", return_value=self.root), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["no-vis"]), 0)
        self.assertIn("[x] profile  alpha", out.getvalue())


class Views(unittest.TestCase):
    """The views that draw (View with hide) leave out a tagged session and its crumbs; bc and the rest (no hide) do not."""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.realpath(tempfile.mkdtemp(prefix="bc-novis-view-"))
        cls.home = os.path.join(cls.root, "profiles", "demo")
        os.makedirs(cls.home)
        p, _ = scenario(cls.home)
        p.shutdown()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def setUp(self):
        novis.save({"sessions": ["s1"]}, self.root)
        self.v = View(os.path.join(self.home, "breadcrumbs"), hide=novis.Hide(self.home, self.root))
        self.addCleanup(self.v.close)

    def test_a_tagged_session_is_left_out(self):
        v = self.v
        s1 = v.refs.get("sess-s1")
        self.assertEqual({n["session"] for n in v.graph()["nodes"]}, {"s0", "s2", "s3"})
        self.assertEqual({s["sid"] for s in v.calendar()["sessions"]}, {"s0", "s2", "s3"})
        self.assertNotIn("s1", {r.get("sid") for r in v.measures()["dims"]["family"]})
        for call in (v.session, v.context, v.venn, lambda sid: v.measures(sid=sid, unit="turn")):
            self.assertRaises(KeyError, call, "s1")
        self.assertRaises(KeyError, v.crumb, s1, 0.5)
        self.assertRaises(KeyError, v.graph, s1)
        self.assertEqual((v.path(s1), v.labels([s1])), ([], {s1: s1}))
        hits = [h["id"] for g in v.find("bike chain") for h in g["hits"]]
        self.assertTrue(hits)
        self.assertNotIn("s1", {v._sid(h, v._crumb(h)) for h in hits})
        rows = [r for r in v.context("s2")["rows"] if r["session"] == "s1"]  # what s2 drew in from s1
        self.assertTrue(rows)
        self.assertEqual([r["label"] for r in rows], [r["id"] for r in rows])
        plain = View(os.path.join(self.home, "breadcrumbs"))  # no hide: bc, vault, core, hive
        try:
            self.assertIn("s1", {n["session"] for n in plain.graph()["nodes"]})
            self.assertTrue(plain.crumb(s1, 0.5)["text"])
        finally:
            plain.close()
        state_db(self.home, [("s1", 1.0, ""), ("s9", 2.0, "")])  # Measures also reads the sessions of state.db
        self.addCleanup(os.remove, os.path.join(self.home, "state.db"))
        family = {r.get("sid") for r in v.measures()["dims"]["family"]}
        self.assertEqual(("s9" in family, "s1" in family), (True, False))

    def test_trail_size_and_a_tagged_profile(self):
        trail = self.v.trail()
        self.assertEqual((trail["session"], len(trail["stack"])), ("s3", 1))  # s3 opened the session crumb of s0
        novis.save({"sessions": ["s0"]}, self.root)
        self.assertEqual(self.v.trail()["stack"], [])
        novis.save({"sessions": ["s3"]}, self.root)
        self.assertEqual(self.v.trail()["session"], "")
        log = os.path.join(self.home, "breadcrumbs", "ace.log")
        self.addCleanup(os.remove, log)
        with open(log, "w", encoding="utf-8") as f:  # Size: ACE's lines of s3 and s2
            f.write("".join("2026-11-11T10:00:0%d pid=1 sess=%s act=pass in=100 out=100 last=0\n" % (i, s)
                            for i, s in enumerate(("s3", "s2"))))
        self.assertEqual((self.v.ace()["all"]["sessions"], self.v.ace("s3")["points"]), (1, []))
        novis.save({"profiles": ["demo"]}, self.root)
        self.assertEqual((self.v.graph()["nodes"], self.v.calendar()["sessions"], self.v.ace()["all"]["sessions"]),
                         ([], [], 0))
        self.assertRaises(KeyError, self.v.session, "s2")


@unittest.skipIf(FASTAPI, FASTAPI)
class Desktop(unittest.TestCase):
    def test_the_desktop_routes_leave_out_what_is_tagged(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="bc-novis-desk-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        home = Path(tmp) / "profiles" / "demo"
        home.mkdir(parents=True)
        p, _ = scenario(str(home))
        p.shutdown()
        api, client = plugin_client(home, tmp)
        self.addCleanup(api.close_all)

        def get(route, **params):
            return client.get(PREFIX + route, params=params)
        self.assertEqual(get("/session", sid="s1").status_code, 200)
        novis.save({"sessions": ["s1"]}, tmp)
        self.assertEqual(get("/session", sid="s1").status_code, 404)  # the open View follows the file
        self.assertNotIn("s1", {n["session"] for n in get("/graph").json()["nodes"]})
        log = api._log_route(types.SimpleNamespace(query_params={}),
                             lambda lv, path, connect: {"sessions": [{"id": "s1"}, {"id": "s2"}]})
        self.assertEqual(log, {"sessions": [{"id": "s2"}]})
        self.assertEqual(api._log_route(types.SimpleNamespace(query_params={}), None, "s1").status_code, 404)
        novis.save({"profiles": ["demo"]}, tmp)
        self.assertEqual(get("/graph").status_code, 403)
        self.assertEqual(get("/stores").json(), {"stores": []})


if __name__ == "__main__":
    unittest.main()
