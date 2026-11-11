import json
import os
import sqlite3
import tempfile
import unittest

from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes_ingest import ingest_state_db
from breadcrumbs.history import Refs
from breadcrumbs.index import Index
from breadcrumbs.ingest import leaves, plain, step_text
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom


def make_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
               " content TEXT, tool_name TEXT, timestamp REAL)")
    db.execute("INSERT INTO sessions VALUES ('s1', 'first session', 100), ('s2', NULL, 200)")
    rows = [("s1", "user", "build the crc32c module", None, 101),
            ("s1", "assistant", "I wrote crc32c.py with RFC 3720 vectors", None, 102),
            ("s1", "tool", "Ran 9 tests in 0.005s\nOK", "terminal", 103),
            ("s2", "user", "now the zoom tool", None, 201),
            ("s2", "assistant", "", None, 202),
            ("s2", "system", "never ingested", None, 203)]
    db.executemany("INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?,?,?,?,?)", rows)
    db.commit()
    db.close()


class HermesIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        make_db(self.db)
        home = os.path.join(self.tmp.name, "bc")
        self.store = Store(os.path.join(home, "store"))
        self.index = Index(os.path.join(home, "index.db"))
        self.refs = Refs(os.path.join(home, "refs"))

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def ingest(self):
        return ingest_state_db(self.db, self.store, self.index, self.refs)

    def test_sessions_become_session_crumbs_in_order(self):
        stats = self.ingest()
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["messages"], 4)  # empty and system rows are skipped
        turns = leaves(self.store, self.refs.get("sess-s1"), kind=None)
        kinds = [get_crumb(self.store, t).kind for t in turns]
        self.assertEqual(kinds, ["turn", "turn", "tool"])

    def test_text_is_exact_and_searchable(self):
        self.ingest()
        hit = self.index.search("RFC 3720 vectors")[0][0]
        self.assertIn("I wrote crc32c.py with RFC 3720 vectors", Zoom(self.store).render(hit, 1.0))

    def test_turns_of_a_session_are_linked_in_order(self):
        self.ingest()
        hits = self.index.search("RFC 3720 vectors", 10, nb=0.3)
        self.assertEqual(len(hits), 3)  # the matching turn, the question before it, the tool row after it

    def test_ingest_twice_adds_nothing(self):
        self.ingest()
        blobs = len(self.store)
        rows = self.index.db.execute("SELECT count(*) FROM rows").fetchone()[0]
        self.ingest()
        self.assertEqual(len(self.store), blobs + 1)  # only the new commit crumb
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM rows").fetchone()[0], rows)

    def test_source_db_is_opened_read_only(self):
        os.chmod(self.db, 0o444)
        try:
            self.ingest()
        finally:
            os.chmod(self.db, 0o644)


CALLS = '[{"id": "call_9", "type": "function", "function": {"name": "read_file", "arguments": "{\\"path\\": \\"notes.txt\\"}"}}]'


class StepIngestTest(unittest.TestCase):
    """A state.db with tool calls: an assistant row that calls tools (it has no text) becomes a "step"
    crumb; a tool row names its call and its step."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL)")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                   " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
        db.execute("INSERT INTO sessions VALUES ('s3', 'notes', 300)")
        db.executemany("INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp)"
                       " VALUES (?,?,?,?,?,?,?)",
                       [("s3", "user", "read the notes", None, None, None, 301),
                        ("s3", "assistant", "", None, CALLS, None, 302),
                        ("s3", "tool", "kettle at eighty degrees", "call_9", None, "read_file", 303),
                        ("s3", "assistant", "The notes say eighty degrees.", None, None, None, 304)])
        db.commit()
        db.close()
        home = os.path.join(self.tmp.name, "bc")
        self.store = Store(os.path.join(home, "store"))
        self.index = Index(os.path.join(home, "index.db"))
        self.refs = Refs(os.path.join(home, "refs"))
        ingest_state_db(self.db, self.store, self.index, self.refs)
        self.crumbs = [(c, get_crumb(self.store, c)) for c in leaves(self.store, self.refs.get("sess-s3"))]

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def test_a_tool_calling_row_becomes_a_step_between_the_turns(self):
        self.assertEqual([c.kind for _, c in self.crumbs], ["turn", "step", "tool", "turn"])
        step_id, step = self.crumbs[1]
        self.assertEqual(Zoom(self.store).source(step_id), '→ read_file path: notes.txt\n')
        self.assertEqual(step.meta["calls"], ["call_9"])

    def test_a_tool_crumb_names_its_call_and_its_step(self):
        tool = self.crumbs[2][1]
        self.assertEqual(tool.meta["role"], "tool:read_file")
        self.assertEqual((tool.meta["call"], tool.meta["step"]), ("call_9", self.crumbs[1][0]))

    def test_a_step_is_searchable_by_its_arguments(self):
        self.assertEqual(self.index.search("notes.txt read_file")[0][0], self.crumbs[1][0])


class FamilyIngestTest(unittest.TestCase):
    """A child session names its parent: the session crumb keeps parent_session_id and source in its meta.
    A state.db whose sessions table lacks a column ingests as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "state.db")
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, parent_session_id TEXT,"
                   " title TEXT, started_at REAL NOT NULL)")
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,"
                   " content TEXT, tool_name TEXT, timestamp REAL)")
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)",
                       [("lead", "cli", None, "the lead", 100), ("kid-a", "subagent", "lead", "part a", 110),
                        ("kid-b", "subagent", "lead", "part b", 120)])
        db.executemany("INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?,?,?,?,?)",
                       [("lead", "user", "split the job in two", None, 101),
                        ("kid-a", "user", "do part a", None, 111), ("kid-a", "assistant", "part a is done", None, 112),
                        ("kid-b", "user", "do part b", None, 121), ("kid-b", "assistant", "part b is done", None, 122)])
        db.commit()
        db.close()
        home = os.path.join(self.tmp.name, "bc")
        self.store = Store(os.path.join(home, "store"))
        self.index = Index(os.path.join(home, "index.db"))
        self.refs = Refs(os.path.join(home, "refs"))

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, db=None):
        return ingest_state_db(db or self.db, self.store, self.index, self.refs)

    def meta(self, sid):
        return get_crumb(self.store, self.refs.get("sess-" + sid)).meta

    def test_two_children_name_their_parent(self):
        self.ingest()
        self.assertEqual(self.meta("kid-a"), {"sess": "kid-a", "parent_session_id": "lead", "source": "subagent"})
        self.assertEqual(self.meta("kid-b"), {"sess": "kid-b", "parent_session_id": "lead", "source": "subagent"})

    def test_a_session_with_no_parent_keeps_only_its_source(self):
        self.ingest()
        self.assertEqual(self.meta("lead"), {"sess": "lead", "source": "cli"})

    def test_a_database_without_those_columns_ingests_as_now(self):
        old = os.path.join(self.tmp.name, "old.db")
        make_db(old)  # sessions (id, title, started_at): no parent_session_id, no source
        self.assertEqual(self.ingest(old)["sessions"], 2)
        self.assertEqual(self.meta("s1"), {"sess": "s1"})
        self.assertEqual(self.meta("s2"), {"sess": "s2"})

    def test_a_database_with_one_of_the_columns_keeps_that_one(self):
        part = os.path.join(self.tmp.name, "part.db")
        make_db(part)
        db = sqlite3.connect(part)
        db.execute("ALTER TABLE sessions ADD COLUMN source TEXT")
        db.execute("UPDATE sessions SET source = 'cli' WHERE id = 's1'")
        db.commit()
        db.close()
        self.ingest(part)
        self.assertEqual(self.meta("s1"), {"sess": "s1", "source": "cli"})
        self.assertEqual(self.meta("s2"), {"sess": "s2"})  # a NULL value gives no key

    def test_ingest_twice_adds_only_the_commit(self):
        self.ingest()
        blobs = len(self.store)
        self.ingest()
        self.assertEqual(len(self.store), blobs + 1)


class PlainTextTest(unittest.TestCase):
    """A crumb keeps the text of a tool result and of call arguments, not their JSON."""

    def test_a_read_file_result_keeps_its_code_lines_as_lines(self):
        result = json.dumps({"content": "1|import os\n2|def f(x):\n3|    return x\n", "total_lines": 3,
                             "hint": None, "truncated": False, "error": ""})
        self.assertEqual(plain(result), "1|import os\n2|def f(x):\n3|    return x\ntotal_lines: 3\ntruncated: false")

    def test_a_terminal_result_gives_its_output_then_the_rest(self):
        result = json.dumps({"output": "a b\nc \"d\"\n", "exit_code": 1, "error": None})
        self.assertEqual(plain(result), 'a b\nc "d"\nexit_code: 1')

    def test_nested_values_go_two_spaces_in_and_other_text_stays(self):
        self.assertEqual(plain('{"ok": true, "hits": [{"id": "a1", "n": 2}], "tags": ["x", "y"]}'),
                         "ok: true\ntags: x, y\nhits:\n  -\n    id: a1\n    n: 2")
        for text in ("plain words", "{not json", "[1, 2", '"a string"', "42"):
            self.assertEqual(plain(text), text)

    def test_a_call_gives_short_arguments_on_its_line_and_long_ones_below(self):
        args = json.dumps({"path": "a.py", "old_string": "x = 1\ny = 2", "replace_all": False})
        self.assertEqual(step_text("I fix it.", [("patch", args), ("todo", "{}"), ("raw", "not json")]),
                         "I fix it.\n\u2192 patch path: a.py \u00b7 replace_all: false\n  old_string:\n  x = 1\n"
                         "  y = 2\n\u2192 todo\n\u2192 raw not json\n")


if __name__ == "__main__":
    unittest.main()
