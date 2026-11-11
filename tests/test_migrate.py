"""migrate: other memory systems' exports become crumbs that keep their source ids (meta src, src_id)."""
import contextlib
import io
import json
import os
import tempfile
import unittest

from breadcrumbs import cli, migrate
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import _ref_name
from breadcrumbs.history import Refs
from breadcrumbs.index import Index
from breadcrumbs.ingest import leaves
from breadcrumbs.store import Store


def node(nid, parent, children, role=None, text=None, t=None):
    msg = None if role is None else {"id": nid, "author": {"role": role}, "create_time": t,
                                     "content": {"content_type": "text", "parts": [text]}}
    return {"id": nid, "message": msg, "parent": parent, "children": children}


# An OpenAI data export: the answer a1 was regenerated as a2; the conversation shows a2 (current_node u2 is on it).
CHATGPT = [{
    "id": "conv-1", "title": "Kettle questions", "create_time": 1767225600.0, "current_node": "u2",
    "mapping": {
        "root": node("root", None, ["sys"]),
        "sys": node("sys", "root", ["u1"], "system", ""),
        "u1": node("u1", "sys", ["a1", "a2"], "user", "where is the walrus kettle?", 1767225601.0),
        "a1": node("a1", "u1", [], "assistant", "on the blue shelf", 1767225602.0),
        "a2": node("a2", "u1", ["u2"], "assistant", "on the green shelf", 1767225603.0),
        "u2": node("u2", "a2", [], "user", "thanks, found it", 1767225604.0),
    }}]

# A claude.ai data export.
CLAUDE = [{"uuid": "c-1", "name": "Garden plan", "created_at": "2026-02-01T10:00:00Z", "chat_messages": [
    {"uuid": "m-1", "sender": "human", "text": "which bed gets the tomatoes?", "created_at": "2026-02-01T10:00:01Z"},
    {"uuid": "m-2", "sender": "assistant", "text": "", "created_at": "2026-02-01T10:00:02Z",
     "content": [{"type": "text", "text": "the south bed, it gets the most sun"}]},
]}]

# A memory list as mem0's get_all returns it.
MEM0 = {"results": [
    {"id": "9b1f-0001", "memory": "prefers green tea in the morning", "created_at": "2026-03-04T05:06:07Z",
     "updated_at": "2026-03-05T05:06:07Z", "user_id": "u-7", "hash": "abc"},
    {"id": "9b1f-0002", "memory": "the bike lock code is not stored here", "created_at": "2026-03-04T05:06:08Z"},
]}


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "bc")
        self.store = Store(os.path.join(self.home, "store"))
        self.index = Index(os.path.join(self.home, "index.db"))
        self.refs = Refs(os.path.join(self.home, "refs"))
        self.addCleanup(self.index.close)
        self.addCleanup(self.store.close)

    def write(self, name, data):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def run_file(self, system, name, data):
        return migrate.migrate_file(self.store, self.index, self.refs, system, self.write(name, data))

    def session(self, sid):
        return [get_crumb(self.store, c) for c in leaves(self.store, self.refs.get(_ref_name("sess-", sid)))]

    def test_a_chatgpt_conversation_becomes_a_session_of_its_visible_branch(self):
        stats = self.run_file("chatgpt", "conversations.json", CHATGPT)
        self.assertEqual((stats["sessions"], stats["messages"]), (1, 3))
        turns = self.session("chatgpt:conv-1")
        texts = [self.text(c) for c in turns]
        self.assertEqual(texts, ["where is the walrus kettle?", "on the green shelf", "thanks, found it"])
        self.assertEqual([(c.meta["src"], c.meta["src_id"], c.meta["role"]) for c in turns],
                         [("chatgpt", "u1", "user"), ("chatgpt", "a2", "assistant"), ("chatgpt", "u2", "user")])
        self.assertEqual(turns[0].meta["ts"], 1767225601)

    def test_a_claude_conversation_becomes_a_session(self):
        self.run_file("claude", "conversations.json", CLAUDE)
        turns = self.session("claude:c-1")
        self.assertEqual([(c.meta["role"], c.meta["src_id"]) for c in turns], [("user", "m-1"), ("assistant", "m-2")])
        self.assertEqual(self.text(turns[1]), "the south bed, it gets the most sun")
        self.assertEqual(turns[0].meta["ts"], 1769940001)  # 2026-02-01T10:00:01Z

    def test_memory_records_become_mem_crumbs_with_their_ids(self):
        stats = self.run_file("mem0", "memories.json", MEM0)
        self.assertEqual(stats["memories"], 2)
        mems = self.session("mem0:memories")
        first = next(c for c in mems if c.meta["src_id"] == "9b1f-0001")
        self.assertEqual((first.kind, first.meta["src"], first.meta["user_id"]), ("mem", "mem0", "u-7"))
        self.assertEqual(first.meta["ts"], 1772600767)  # 2026-03-04T05:06:07Z
        self.assertEqual(self.text(first), "prefers green tea in the morning")
        top = get_crumb(self.store, self.index.search("green tea morning", 3)[0][0])
        self.assertEqual(top.meta["src_id"], "9b1f-0001")

    def test_hermes_memory_entries_become_mem_crumbs(self):
        path = self.write("MEMORY.md", "The build host runs the load tests.\n\u00a7\nThe laptop sleeps at night.\n")
        os.utime(path, (1767225600, 1767225600))
        stats = migrate.migrate_file(self.store, self.index, self.refs, "hermes", path)
        self.assertEqual(stats["memories"], 2)
        mems = self.session("hermes:memories")
        self.assertEqual(sorted(c.meta["src_id"] for c in mems), ["MEMORY.md#1", "MEMORY.md#2"])
        self.assertEqual({c.meta["ts"] for c in mems}, {1767225600})  # no time in the file: the file's time

    def test_a_second_run_adds_no_crumb_and_no_index_row(self):
        self.run_file("chatgpt", "conversations.json", CHATGPT)
        self.run_file("mem0", "memories.json", MEM0)
        rows = self.index.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
        refs = {n: self.refs.get(n) for n in self.refs.names() if n.startswith("sess-")}
        self.run_file("chatgpt", "conversations.json", CHATGPT)
        self.run_file("mem0", "memories.json", MEM0)
        self.assertEqual(self.index.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0], rows)
        self.assertEqual({n: self.refs.get(n) for n in self.refs.names() if n.startswith("sess-")}, refs)

    def test_the_cli_migrates_a_file(self):
        path, home, out = self.write("memories.json", MEM0), os.path.join(self.tmp.name, "cli-home"), io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", home, "migrate", "mem0", path])
        self.assertEqual(code or 0, 0)
        self.assertEqual(json.loads(out.getvalue())["memories"], 2)
        self.assertTrue(Refs(os.path.join(home, "refs")).get(_ref_name("sess-", "mem0:memories")))

    def text(self, crumb):
        doc, a, b = crumb.ranges[0]
        from breadcrumbs.lines import get_lines
        return b"".join(get_lines(self.store, doc, a, b)).decode("utf-8")


if __name__ == "__main__":
    unittest.main()
