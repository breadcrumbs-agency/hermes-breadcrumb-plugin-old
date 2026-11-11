"""prune-index: drop the index rows that older versions made for the calls and results of this memory's
own tools (they copy memory and rank above their source). A pruned index equals one built with the
current rules: the same rows, neighbor links and term counts."""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from breadcrumbs import cli, ingest
from breadcrumbs.index import Index
from breadcrumbs.store import Store


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


ROWS = [
    {"role": "user", "content": "where is the walrus kettle?"},
    {"role": "assistant", "content": None, "tool_calls": [call("c1", "mcp__crumb_find", '{"q": "walrus kettle"}')]},
    {"role": "tool", "tool_call_id": "c1", "name": "mcp__crumb_find", "content": "walrus kettle hits from memory"},
    {"role": "assistant", "content": "Let me read the file.", "tool_calls": [call("c2", "terminal", '{"command": "cat w"}')]},
    {"role": "tool", "tool_call_id": "c2", "name": "terminal", "content": "walrus kettle file text"},
    {"role": "assistant", "content": None,
     "tool_calls": [call("c3", "crumb", '{"id": "x"}'), call("c4", "terminal", '{"command": "ls"}')]},  # mixed
    {"role": "tool", "tool_call_id": "c3", "name": "crumb", "content": "walrus kettle crumb text"},
    {"role": "tool", "tool_call_id": "c4", "name": "terminal", "content": "walrus kettle second file"},
    {"role": "assistant", "content": "The walrus kettle is on the shelf."},
]


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def build(self, name, old, keep_open=True):
        root = os.path.join(self.tmp.name, name)
        store, index = Store(os.path.join(root, "store")), Index(os.path.join(root, "index.db"))
        if keep_open:
            self.addCleanup(store.close)
            self.addCleanup(index.close)
        with mock.patch.object(ingest, "MEMORY_TOOLS", frozenset() if old else ingest.MEMORY_TOOLS):
            ingest.put_rows(store, index, ROWS, "s1", ts=1000)
        index.commit()
        store.flush()
        return root, store, index

    @staticmethod
    def shape(index):
        index.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.terms USING fts5vocab('main', 't', 'row')")
        out = (set(index.db.execute("SELECT crumb, ref, kind, public FROM rows")),
               set(index.db.execute("SELECT a, b FROM nbr")),
               sorted(index.db.execute("SELECT term, doc, cnt FROM temp.terms")))
        index.db.commit()
        return out

    def test_a_pruned_index_equals_one_built_with_the_current_rules(self):
        _, store, old = self.build("old", old=True)
        _, _, new = self.build("new", old=False)
        self.assertNotEqual(self.shape(old), self.shape(new))
        self.assertEqual(ingest.prune_index(store, old), (3, 3))  # 1 memory step, 2 memory results
        self.assertEqual(self.shape(old), self.shape(new))
        self.assertEqual(ingest.prune_index(store, old), (0, 0))

    def test_the_cli_prunes_and_says_what_it_removed(self):
        root, store, index = self.build("old", old=True, keep_open=False)
        store.close()
        index.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", root, "prune-index"]), 0)
        self.assertIn("3 crumbs, 3 index rows", out.getvalue())


if __name__ == "__main__":
    unittest.main()
