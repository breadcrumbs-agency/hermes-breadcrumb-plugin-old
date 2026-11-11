"""The dial `family` (breadcrumbs/family.py and BreadcrumbsProvider.on_delegation): the rows of a delegate_task
child go from state.db to the child's own store and a CAR file.

The fixture state.db has one parent (p1), three children of it (c1, c2, c3) and a session that is nobody's child (x1).
The text is neutral. c3 has one tool row with an empty result, so a store keeps no turn of it. With the dial off (the
default), the provider opens no database and writes no file."""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, family
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.index import Index
from breadcrumbs.share import import_car, read_car
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom

try:
    from hermes_state import SessionDB
except ImportError:  # not the Python of a Hermes install
    SessionDB = None

CALL = json.dumps([{"id": "call_1", "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "kettle.txt"})}}])


def make_state_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, parent_session_id TEXT, title TEXT,"
               " started_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
               " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)",
                   [("p1", "cli", None, "the parent", 100), ("c1", "subagent", "p1", None, 110),
                    ("c2", "subagent", "p1", "shelf", 111), ("c3", "subagent", "p1", None, 112),
                    ("x1", "cli", None, "other", 120)])
    db.executemany("INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp)"
                   " VALUES (?,?,?,?,?,?,?)",
                   [("p1", "user", "split the job in two", None, None, None, 101),
                    ("c1", "user", "measure the kettle", None, None, None, 111),
                    ("c1", "assistant", "", None, CALL, None, 112),
                    ("c1", "tool", "kettle at eighty degrees", "call_1", None, "read_file", 113),
                    ("c1", "assistant", "The kettle is at eighty degrees.", None, None, None, 114),
                    ("c2", "user", "check the shelf", None, None, None, 112),
                    ("c2", "assistant", "The shelf is made of oak.", None, None, None, 113),
                    ("c3", "tool", "{}", "call_2", None, "read_file", 114),
                    ("x1", "user", "walrus chatter that belongs to no child", None, None, None, 121)])
    db.commit()
    db.close()


class FamilyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.root = os.path.join(self.home, "breadcrumbs")
        make_state_db(os.path.join(self.home, "state.db"))

    def provider(self, cfg=None, context="primary"):
        if cfg is not None:
            os.makedirs(self.root, exist_ok=True)
            with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        p = BreadcrumbsProvider()
        p.initialize("p1", hermes_home=self.home, platform="cli", agent_context=context)
        self.addCleanup(p.shutdown)
        return p

    def children(self):
        return os.path.join(self.root, "children")

    def log(self):
        with open(os.path.join(self.root, "provider.log"), encoding="utf-8") as f:
            return f.read()

    def export(self, p, sid):
        got = p._child_car(sid)
        assert got is not None, sid  # the export of a child that has rows
        return got


class ExportTest(FamilyCase):
    def test_one_parent_and_two_children_give_two_cars_that_read_car_accepts(self):
        p = self.provider({"family": True})
        self.assertTrue(p.family)
        for sid in ("c1", "c2"):
            p.on_delegation("a task", "its summary", child_session_id=sid)
        cids = {}
        for sid, mine, other in (("c1", b"kettle at eighty degrees", b"made of oak"),
                                 ("c2", b"made of oak", b"kettle at eighty")):
            car = os.path.join(self.children(), sid, "share", sid + ".car")
            self.assertTrue(os.path.isfile(car), sid)
            roots, blocks = read_car(car)  # it checks every block against its CID
            self.assertEqual(len(roots), 1)
            self.assertEqual(json.loads(blocks[roots[0]])["kind"], "sess")
            with open(car, "rb") as f:
                data = f.read()
            self.assertIn(mine, data)
            self.assertNotIn(other, data)  # the other child
            self.assertNotIn(b"walrus", data)  # a session that is nobody's child
            self.assertNotIn(b"split the job", data)  # the parent
            cids[sid] = roots[0]
        self.assertNotEqual(cids["c1"], cids["c2"])
        self.assertEqual(sorted(os.listdir(self.children())), ["c1", "c2"])

    def test_the_child_store_is_a_home_that_verifies_and_the_car_opens_in_another_store(self):
        p = self.provider({"family": True})
        got = self.export(p, "c1")
        home = os.path.join(self.children(), "c1")
        self.assertEqual((got["sid"], got["home"], got["messages"]), ("c1", home, 4))  # user, step, tool, answer
        self.assertEqual(got["car"], os.path.join(home, "share", "c1.car"))
        self.assertEqual(got["cid"], read_car(got["car"])[0][0])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", home, "verify"]), 0)
        self.assertEqual(json.loads(out.getvalue())["bad"], 0)
        with Store(os.path.join(self.home, "other")) as other:  # the CAR is the transport
            index = Index(":memory:")
            try:
                root = import_car(other, index, got["car"])[0]
                self.assertIn("kettle at eighty degrees", Zoom(other).render(root, 1.0))
            finally:
                index.close()

    def test_the_same_child_again_gives_the_same_car(self):
        p = self.provider({"family": True})
        first = self.export(p, "c2")
        with open(first["car"], "rb") as f:
            before = f.read()
        again = self.export(p, "c2")
        with open(again["car"], "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual((again["cid"], again["root"]), (first["cid"], first["root"]))


class DialOffTest(FamilyCase):
    def test_with_the_dial_off_nothing_is_read_and_nothing_is_written(self):
        p = self.provider()  # no config.json: the default
        self.assertIs(p.family, False)
        with mock.patch("sqlite3.connect", side_effect=AssertionError("a database was opened")):
            p.on_delegation("a task", "its summary", child_session_id="c1")
        self.assertFalse(os.path.exists(self.children()))

    def test_the_patch_of_that_test_does_catch_a_read_when_the_dial_is_on(self):
        p = self.provider({"family": True})
        with mock.patch("sqlite3.connect", side_effect=AssertionError("a database was opened")):
            p.on_delegation("a task", "its summary", child_session_id="c1")  # logged, not raised
        self.assertIn("family: child c1: AssertionError", self.log())
        self.assertFalse(os.path.exists(self.children()))

    def test_a_provider_that_may_not_write_reads_and_writes_nothing(self):
        p = self.provider({"family": True}, context="subagent")
        self.assertTrue(p.family)
        self.assertFalse(p.writable)
        with mock.patch("sqlite3.connect", side_effect=AssertionError("a database was opened")):
            p.on_delegation("a task", "its summary", child_session_id="c1")
        self.assertFalse(os.path.exists(self.children()))


class NothingToExportTest(FamilyCase):
    def test_a_child_with_no_rows_or_a_bad_id_leaves_nothing_behind(self):
        p = self.provider({"family": True})
        for sid in ("nobody", "", "../c1", "c1/../c2", ".hidden", "a" * 200):
            p.on_delegation("a task", "its summary", child_session_id=sid)
        self.assertFalse(os.path.exists(self.children()))
        self.assertIsNone(family.child_home(self.root, "../c1"))
        self.assertEqual(family.child_home(self.root, "20261110_120000_ab12cd"),
                         os.path.join(self.root, "children", "20261110_120000_ab12cd"))

    def test_no_state_db_is_logged_and_never_raised(self):
        p = self.provider({"family": True})
        os.remove(os.path.join(self.home, "state.db"))
        p.on_delegation("a task", "its summary", child_session_id="c1")
        self.assertIn("family: child c1: FileNotFoundError", self.log())
        self.assertFalse(os.path.exists(self.children()))

    def test_a_child_whose_rows_keep_no_turn_leaves_no_folder_and_is_logged(self):
        p = self.provider({"family": True})
        p.on_delegation("a task", "its summary", child_session_id="c3")  # one tool row: its result is {}
        self.assertFalse(os.path.exists(self.children()))
        self.assertIn("family: child c3: no rows to keep", self.log())
        p.on_delegation("a task", "its summary", child_session_id="nobody")
        self.assertIn("family: child nobody: no rows to keep", self.log())

    def test_a_folder_that_was_there_before_is_never_removed(self):
        p = self.provider({"family": True})
        note = os.path.join(self.children(), "c3", "note.txt")
        os.makedirs(os.path.dirname(note))
        with open(note, "w", encoding="utf-8") as f:
            f.write("mine")
        self.assertIsNone(p._child_car("c3"))
        self.assertTrue(os.path.isfile(note))


@unittest.skipIf(SessionDB is None, "needs the Python of a Hermes install (hermes_state)")
class RealStateDbTest(FamilyCase):
    """The same export from a state.db that Hermes's own SessionDB wrote: the columns of the real schema."""

    def setUp(self):
        super().setUp()
        self.db_file = os.path.join(self.home, "state.db")
        os.remove(self.db_file)  # the minimal fixture of the other tests
        # No HERMES_HOME here: a new data root makes Hermes build a dependency environment for it (hundreds of MB).
        # SessionDB writes only to db_path, as in test_logview.py.
        assert SessionDB is not None  # the class is skipped without it
        db = SessionDB(db_path=Path(self.db_file))
        try:
            db.create_session("p1", "cli", model="test")
            db.create_session("r1", "subagent", model="test", parent_session_id="p1")
            db.append_message("r1", "user", "measure the kettle")
            db.append_message("r1", "assistant", "", tool_calls=[
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": "kettle.txt"})}}])
            db.append_message("r1", "tool", "kettle at eighty degrees", tool_name="read_file", tool_call_id="call_1")
            db.append_message("r1", "assistant", "The kettle is at eighty degrees.")
            db.append_message("p1", "user", "walrus chatter of the parent")
        finally:
            db.close()

    def test_a_real_child_gives_a_car_and_state_db_stays_as_it_was(self):
        before = hashlib.sha256(Path(self.db_file).read_bytes()).hexdigest()
        p = self.provider({"family": True})
        got = self.export(p, "r1")
        self.assertEqual(got["messages"], 4)  # user, step, tool, answer
        roots, blocks = read_car(got["car"])
        text = b"\n".join(blocks.values())
        self.assertIn(b"kettle at eighty degrees", text)
        self.assertIn(b"read_file", text)  # the step: its call, in plain text
        self.assertNotIn(b"walrus", text)  # the parent
        self.assertEqual(roots, [got["cid"]])
        self.assertEqual(hashlib.sha256(Path(self.db_file).read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
