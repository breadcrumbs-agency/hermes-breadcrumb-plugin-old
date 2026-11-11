"""The demo log of spacegraph/script/seed-demo-log.py, on state.db files that Hermes's own SessionDB writes.

The seed runs twice, each time in a fresh Python, into two temporary folders. The tests read the two
files read-only. They check that the runs give the same rows, that the compaction has the rows of a
Hermes compaction, that the one session_search call has the scroll shape and its real output, and
that no row holds a SpaceGraph block.
Run with the Python of a Hermes install (it has hermes_state); without it the suite skips.
"""
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "spacegraph" / "script" / "seed-demo-log.py"  # not in the release: the tests skip where it is missing
OLD_HEADER = "SpaceGraph log references"  # the block header of the removed log-reference layer (commit 5331f06)
IDENTITY = "state_meta"  # Hermes mints each new file's identity here (a uuid, the creation time)
TODAY = "Sunday chores"

try:
    from hermes_state import SessionDB
except ImportError:  # not the Python of a Hermes install
    SessionDB = None


def local(*fields) -> float:
    return time.mktime(tuple(fields) + (0,) * (6 - len(fields)) + (0, 0, -1))


def run_seed(top):
    return subprocess.run([sys.executable, str(SEED), str(top)], capture_output=True, text=True, timeout=300)


def seed(top) -> Path:
    run = run_seed(top)
    if run.returncode:
        raise AssertionError("the seed failed (%d): %s%s" % (run.returncode, run.stdout, run.stderr))
    return Path(top) / "state.db"


def connect(db):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows(db, sql, args=()):
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def tables(db) -> dict:
    """Every table of a state.db: {name: rows}."""
    conn = connect(db)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {name: [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}"').fetchall()] for name in names}
    finally:
        conn.close()


STATE = {}  # the two seeded files, made once for the module


def setUpModule():
    if SessionDB is None or not SEED.is_file():
        return
    STATE["tmp"] = tempfile.TemporaryDirectory(prefix="sg-demo-")
    top = Path(STATE["tmp"].name)
    STATE["db"], STATE["db2"] = seed(top / "a"), seed(top / "b")


def tearDownModule():
    if "tmp" in STATE:
        STATE.pop("tmp").cleanup()


@unittest.skipIf(SessionDB is None, "needs the Python of a Hermes install (hermes_state)")
@unittest.skipUnless(SEED.is_file(), "spacegraph/script/seed-demo-log.py is not in the release")
class DemoLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.top, cls.db, cls.db2 = Path(STATE["tmp"].name), STATE["db"], STATE["db2"]
        cls.sids = {r["title"]: r["id"] for r in rows(cls.db, "SELECT id, title FROM sessions")}
        cls.msgs = rows(cls.db, "SELECT * FROM messages ORDER BY id")
        cls.today = [m for m in cls.msgs if m["session_id"] == cls.sids[TODAY]]

    def calls(self, name):
        """(assistant row, tool call) for each call of the tool ``name`` in the session of today."""
        return [(m, c) for m in self.today if m["tool_calls"] for c in json.loads(m["tool_calls"])
                if c["function"]["name"] == name]


class SameEachRun(DemoLogTest):
    def test_two_runs_write_the_same_rows(self):
        a, b = tables(self.db), tables(self.db2)
        self.assertEqual(sorted(a), sorted(b))
        for name in a:
            if name != IDENTITY:
                self.assertEqual(a[name], b[name], name)
        self.assertEqual(sorted(r[0] for r in a[IDENTITY]), sorted(r[0] for r in b[IDENTITY]))
        key = "SELECT id, session_id, role, content, api_content, active, compacted FROM messages ORDER BY id"
        self.assertEqual(rows(self.db, key), rows(self.db2, key))
        self.assertEqual(len(a["messages"]), 34)

    def test_the_times_are_fixed(self):
        start, end = local(2026, 11, 1, 9, 0), local(2026, 11, 8, 10, 5, 14)  # the first and the last message
        stamps = [m["timestamp"] for m in self.msgs]
        self.assertEqual((min(stamps), max(stamps)), (start, end))
        self.assertTrue(all(float(t).is_integer() for t in stamps))  # whole seconds: no reading of a clock
        started = {r["title"]: r["started_at"] for r in rows(self.db, "SELECT title, started_at FROM sessions")}
        self.assertEqual(started, {"Bike repair": start, "Sourdough bread": local(2026, 11, 3, 9, 0),
                                   "Balcony garden": local(2026, 11, 5, 9, 0), TODAY: local(2026, 11, 8, 9, 0)})

    def test_the_sessions_have_hermes_ids_and_titles(self):
        order = [r["title"] for r in rows(self.db, "SELECT title FROM sessions ORDER BY started_at")]
        self.assertEqual(order, ["Bike repair", "Sourdough bread", "Balcony garden", TODAY])
        for sid in self.sids.values():
            self.assertRegex(sid, r"^\d{8}_\d{6}_[0-9a-f]{6}$")
        users = [m for m in self.today if m["role"] == "user" and not m["_compressed_summary"] and m["compacted"] + m["active"]]
        self.assertEqual(len(users), 5)


class Compaction(DemoLogTest):
    def test_the_compaction_rows_exist(self):
        summary = [m for m in self.today if m["_compressed_summary"]]
        self.assertEqual(len(summary), 1)
        s = summary[0]
        self.assertEqual((s["role"], s["active"], s["compacted"]), ("user", 1, 0))
        from agent.context_compressor import is_compaction_summary_message
        self.assertTrue(is_compaction_summary_message(s["content"]))  # Hermes reads it as its own handoff
        before = [m for m in self.today if m["id"] < s["id"]]
        after = [m for m in self.today if m["id"] > s["id"]]
        archived = [m for m in before if (m["active"], m["compacted"]) == (0, 1)]
        superseded = [m for m in before if (m["active"], m["compacted"]) == (0, 0)]
        self.assertEqual(len(archived) + len(superseded), len(before))  # nothing before the summary is live
        self.assertTrue(all(m["active"] == 1 and m["compacted"] == 0 for m in after))
        self.assertEqual([m["role"] for m in superseded], ["assistant", "tool", "assistant"])
        self.assertEqual(superseded, before[-3:])  # the tail: the last tool round
        self.assertEqual(len(archived), 9)  # turns 1 and 2, and the user row of turn 3
        copies = after[:3]
        same = ("role", "content", "tool_calls", "tool_call_id", "tool_name", "timestamp", "message_uid")
        for old, new in zip(superseded, copies):
            self.assertEqual([old[k] for k in same], [new[k] for k in same])
        self.assertIn("session_search", {m["tool_name"] for m in archived})

    def test_hermes_restores_the_summary_and_the_tail_as_the_context(self):
        db = SessionDB(db_path=self.db, read_only=True)
        try:
            context = db.get_messages_as_conversation(self.sids[TODAY])
            display = db.get_messages(self.sids[TODAY], include_compacted=True)
        finally:
            db.close()
        live = [m for m in self.today if m["active"]]
        self.assertEqual([(m["role"], m.get("content") or "") for m in context],
                         [(m["role"], m["content"] or "") for m in live])
        self.assertTrue(context[0]["content"].startswith("[CONTEXT COMPACTION"))
        self.assertGreater(len(display), len(context))  # the archived turns stay in the display history


class Tools(DemoLogTest):
    def test_one_session_search_call_in_the_scroll_shape(self):
        found = self.calls("session_search")
        self.assertEqual(len(found), 1)
        assistant, call = found[0]
        args = json.loads(call["function"]["arguments"])
        self.assertEqual(sorted(args), ["around_message_id", "session_id", "window"])
        self.assertEqual(args["session_id"], self.sids["Bike repair"])
        anchor = [m for m in self.msgs if m["id"] == args["around_message_id"]][0]
        self.assertEqual((anchor["session_id"], anchor["role"]), (self.sids["Bike repair"], "assistant"))
        tool = [m for m in self.today if m["tool_call_id"] == call["id"]]
        self.assertEqual(len(tool), 1)
        self.assertEqual((tool[0]["role"], tool[0]["tool_name"]), ("tool", "session_search"))
        out = json.loads(tool[0]["content"])
        self.assertEqual((out["success"], out["mode"], out["session_id"]), (True, "scroll", args["session_id"]))
        self.assertTrue(any(m.get("id") == anchor["id"] and m.get("anchor") is True for m in out["messages"]))
        user = [m for m in self.today if m["role"] == "user" and m["id"] < assistant["id"]][-1]
        self.assertIn(f"session {args['session_id']}, message {args['around_message_id']}", user["content"])

    def test_the_tool_row_is_the_real_output_of_session_search(self):
        from tools.session_search_tool import session_search
        assistant, call = self.calls("session_search")[0]
        args = json.loads(call["function"]["arguments"])
        tool = [m for m in self.today if m["tool_call_id"] == call["id"]][0]
        db = SessionDB(db_path=self.db, read_only=True)
        try:
            again = session_search(**args, db=db, current_session_id=self.sids[TODAY])
        finally:
            db.close()
        self.assertEqual(again, tool["content"])

    def test_each_tool_row_follows_its_call(self):
        tools = [i for i, m in enumerate(self.today) if m["role"] == "tool"]
        self.assertEqual(len(tools), 5)  # 3 todo_list rounds, 1 session_search, 1 live copy of a todo_list round
        for i in tools:
            tool, assistant = self.today[i], self.today[i - 1]
            calls = {c["id"]: c for c in json.loads(assistant["tool_calls"] or "[]")}
            self.assertIn(tool["tool_call_id"], calls)
            self.assertEqual(calls[tool["tool_call_id"]]["function"]["name"], tool["tool_name"])
            self.assertEqual(json.loads(assistant["tool_call_uids"])[tool["tool_call_id"]], tool["tool_call_uid"])
        names = [m["tool_name"] for m in self.today if m["role"] == "tool" and m["active"] + m["compacted"]]
        self.assertEqual(names, ["todo_list", "session_search", "todo_list", "todo_list"])
        last = json.loads(self.today[tools[-1]]["content"])
        self.assertEqual(last["summary"], {"total": 4, "pending": 1, "in_progress": 0, "completed": 3,
                                           "cancelled": 0})


class Neutral(DemoLogTest):
    def test_no_row_holds_a_spacegraph_block(self):
        for name, table in tables(self.db).items():
            for row in table:
                for value in row:
                    if isinstance(value, str):
                        self.assertNotIn(OLD_HEADER, value, name)
                    elif isinstance(value, bytes):
                        self.assertNotIn(OLD_HEADER.encode(), value, name)
        self.assertTrue(all(m["api_content"] is None for m in self.msgs))  # nothing was injected anywhere

    def test_no_row_names_this_machine(self):
        home = str(Path.home())
        for name, table in tables(self.db).items():
            for row in table:
                self.assertFalse(any(isinstance(v, str) and home in v for v in row), name)

    def test_the_seed_imports_no_spacegraph_and_no_breadcrumbs_code(self):
        text = SEED.read_text()
        self.assertIsNone(re.search(r"^\s*(from|import)\s+(spacegraph|breadcrumbs)\b", text, re.M))

    def test_the_seed_refuses_a_folder_with_a_state_db_or_a_config(self):
        for name in ("state.db", "config.yaml"):
            top = self.top / ("refuse-" + name)
            top.mkdir()
            (top / name).write_bytes(b"keep")
            run = run_seed(top)
            self.assertNotEqual(run.returncode, 0, name)
            self.assertIn("refused", run.stderr)
            self.assertEqual(sorted(p.name for p in top.iterdir()), [name])
            self.assertEqual((top / name).read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
