import os
import tempfile
import unittest

from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.history import Refs, commit, log
from breadcrumbs.ingest import put_tree, put_turn
from breadcrumbs.index import Index
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.refs = Refs(os.path.join(self.tmp.name, "refs"))
        self.a = put_crumb(self.store, Crumb("mem", "a"))
        self.b = put_crumb(self.store, Crumb("mem", "b"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_commit_moves_the_ref_and_links_the_parent(self):
        c1 = commit(self.store, self.refs, "main", [self.a], "first", ts=1)
        c2 = commit(self.store, self.refs, "main", [self.b], "second", ts=2)
        self.assertEqual(self.refs.get("main"), c2)
        self.assertEqual(get_crumb(self.store, c2).heritage, [c1])
        self.assertEqual([cid for cid, _ in log(self.store, c2)], [c2, c1])

    def test_ref_survives_a_new_refs_object(self):
        c1 = commit(self.store, self.refs, "main", [self.a], "first", ts=1)
        self.assertEqual(Refs(os.path.join(self.tmp.name, "refs")).get("main"), c1)

    def test_commit_id_covers_roots_parent_and_message(self):
        c1 = commit(self.store, self.refs, "x", [self.a], "m", ts=5)
        c2 = commit(self.store, self.refs, "y", [self.a], "m", ts=5)
        self.assertEqual(c1, c2)  # same content, no parent, same time: same id
        c3 = commit(self.store, self.refs, "z", [self.b], "m", ts=5)
        self.assertNotEqual(c1, c3)

    def test_unknown_ref_is_none(self):
        self.assertIsNone(self.refs.get("nope"))


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.index = Index(os.path.join(self.tmp.name, "index.db"))
        self.zoom = Zoom(self.store)

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def test_turn_crumb_holds_the_exact_text_and_is_searchable(self):
        t = put_turn(self.store, self.index, "user", "please pin the graalpy flags\nthanks", "s1", ts=10)
        c = get_crumb(self.store, t)
        self.assertEqual(c.kind, "turn")
        self.assertEqual(c.meta["role"], "user")
        self.assertEqual(self.zoom.render(c.ranges[0][0], 1.0), "please pin the graalpy flags\nthanks")
        self.assertEqual(self.index.search("graalpy")[0][0], t)

    def test_tree_keeps_fanout_and_order(self):
        turns = [put_turn(self.store, self.index, "user", "message number %d" % i, "s1", ts=i) for i in range(40)]
        root = put_tree(self.store, "sess", "session one", turns, fanout=8)
        c = get_crumb(self.store, root)
        self.assertLessEqual(len(c.kids), 8)
        flat = []

        def walk(cid):
            node = get_crumb(self.store, cid)
            if node.kind == "turn":
                flat.append(cid)
            for k in node.kids:
                walk(k)
        walk(root)
        self.assertEqual(flat, turns)

    def test_small_tree_is_one_crumb(self):
        turns = [put_turn(self.store, self.index, "user", "m%d" % i, "s1", ts=i) for i in range(3)]
        self.assertEqual(get_crumb(self.store, put_tree(self.store, "sess", "s", turns)).kids, turns)


if __name__ == "__main__":
    unittest.main()
