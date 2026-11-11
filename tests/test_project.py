"""Shared projects through IPFS CAR files. An imported crumb exports again to the same CIDs, so two
stores that accept one seed hold the same crumb ids. A project head grows by adding public copies;
a pull fast-forwards, and two diverged heads join to one head that both sides compute alike."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.history import Refs
from breadcrumbs.index import Index
from breadcrumbs.lines import put_text
from breadcrumbs.share import export_car, import_car, project_add, publish, pull, push, read_car
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IPFS = shutil.which("ipfs")


class Peer:
    """One agent's memory: a store, its index and its refs."""

    def __init__(self, root):
        self.store = Store(os.path.join(root, "store"))
        self.index = Index(os.path.join(root, "index.db"))
        self.refs = Refs(os.path.join(root, "refs"))

    def note(self, text, title):
        """A private turn whose range starts at line 1 of its text (so the line offset matters)."""
        doc = put_text(self.store, ("header line\n%s\n" % text).encode())
        cid = put_crumb(self.store, Crumb("turn", title, ranges=[[doc, 1, 2]], meta={"role": "user", "ts": 7}))
        self.index.add(cid, "%s:1-2" % doc, text, kind="turn")
        self.index.commit()
        return cid

    def close(self):
        self.index.close()
        self.store.close()


class Peers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.a, self.b, self.c = (Peer(os.path.join(self.tmp.name, n)) for n in "abc")
        self.n = 0

    def tearDown(self):
        for p in (self.a, self.b, self.c):
            p.close()
        self.tmp.cleanup()

    def car(self):
        self.n += 1
        return os.path.join(self.tmp.name, "%d.car" % self.n)


class RoundTripTest(Peers):
    def setUp(self):
        super().setUp()
        x = self.a.note("the kettle rests three minutes", "kettle")
        self.root = publish(self.a.store, put_crumb(self.a.store, Crumb("sess", "tea", kids=[x])), deep=True)
        self.seed = self.car()
        self.roots, _ = export_car(self.a.store, [self.root], self.seed)

    def test_an_import_exports_again_to_the_same_cids(self):
        local = import_car(self.b.store, self.b.index, self.seed)
        again = self.car()
        roots, _ = export_car(self.b.store, local, again)
        self.assertEqual(roots, self.roots)
        self.assertEqual(read_car(again)[1], read_car(self.seed)[1])

    def test_two_stores_that_accept_one_seed_hold_the_same_ids(self):
        ids = [import_car(p.store, p.index, self.seed) for p in (self.a, self.b, self.c)]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[1], ids[2])

    def test_an_opened_crumb_from_ipfs_names_its_source_cid(self):
        local = import_car(self.b.store, self.b.index, self.seed)[0]
        zoom = Zoom(self.b.store)
        self.assertTrue(zoom.render(local, 1.0).splitlines()[0].endswith(" \u21d0 " + self.roots[0]))
        self.assertNotIn(self.roots[0], zoom.handle(local))  # a handle in a list stays short


class ProjectTest(Peers):
    def test_add_makes_a_public_head_over_the_old_one(self):
        x, y = self.a.note("alpha fact", "x"), self.a.note("beta fact", "y")
        h1 = project_add(self.a.store, self.a.refs, "p", [x])
        h2 = project_add(self.a.store, self.a.refs, "p", [y])
        c1, c2 = get_crumb(self.a.store, h1), get_crumb(self.a.store, h2)
        self.assertEqual((c2.kind, c2.title, c2.heritage, c2.public), ("proj", "p", [h1], True))
        self.assertEqual(c2.kids[:1], c1.kids)
        self.assertEqual(len(c2.kids), 2)
        self.assertTrue(all(get_crumb(self.a.store, k).public for k in c2.kids))
        self.assertFalse(get_crumb(self.a.store, x).public)  # the private original stays private
        self.assertEqual(self.a.refs.get("proj-p"), h2)

    def test_a_pull_fast_forwards_to_a_head_that_descends_from_ours(self):
        project_add(self.a.store, self.a.refs, "p", [self.a.note("alpha fact", "x")])
        seed = self.car()
        _, h1 = push(self.a.store, self.a.refs, "p", seed)
        self.assertEqual(pull(self.b.store, self.b.index, self.b.refs, "p", seed), h1)  # one id on both sides
        project_add(self.b.store, self.b.refs, "p", [self.b.note("beta fact", "y")])
        mine = self.car()
        _, h2 = push(self.b.store, self.b.refs, "p", mine)
        self.assertEqual(pull(self.a.store, self.a.index, self.a.refs, "p", mine), h2)
        self.assertEqual(get_crumb(self.a.store, h2).heritage, [h1])

    def test_diverged_heads_join_to_the_same_head_on_both_sides(self):
        project_add(self.a.store, self.a.refs, "p", [self.a.note("alpha fact", "x")])
        seed = self.car()
        push(self.a.store, self.a.refs, "p", seed)
        pull(self.b.store, self.b.index, self.b.refs, "p", seed)
        project_add(self.a.store, self.a.refs, "p", [self.a.note("gamma from a", "ga")])
        project_add(self.b.store, self.b.refs, "p", [self.b.note("delta from b", "db")])
        ca, cb = self.car(), self.car()
        push(self.a.store, self.a.refs, "p", ca)
        push(self.b.store, self.b.refs, "p", cb)
        ha = pull(self.a.store, self.a.index, self.a.refs, "p", cb)
        hb = pull(self.b.store, self.b.index, self.b.refs, "p", ca)
        self.assertEqual(ha, hb)
        joined = get_crumb(self.a.store, ha)
        self.assertEqual((len(joined.kids), len(joined.heritage)), (3, 2))

    def test_a_peer_finds_the_text_it_pulled(self):
        project_add(self.a.store, self.a.refs, "p", [self.a.note("the cassette is worn", "x")])
        c = self.car()
        push(self.a.store, self.a.refs, "p", c)
        pull(self.b.store, self.b.index, self.b.refs, "p", c)
        self.assertTrue(self.b.index.search("cassette worn", 5))

    def test_a_pull_never_indexes_a_crumb_this_store_holds(self):
        project_add(self.a.store, self.a.refs, "p", [self.a.note("the cassette is worn", "x")])
        seed = self.car()
        push(self.a.store, self.a.refs, "p", seed)
        pull(self.b.store, self.b.index, self.b.refs, "p", seed)
        project_add(self.b.store, self.b.refs, "p", [self.b.note("the chain is new", "y")])
        back = self.car()
        push(self.b.store, self.b.refs, "p", back)
        for _ in range(2):  # the second pull of the same head adds nothing
            pull(self.a.store, self.a.index, self.a.refs, "p", back)
        self.assertEqual(len(self.a.index.search("cassette worn", 10)), 1)  # the original only
        self.assertEqual(len(self.a.index.search("chain new", 10)), 1)  # the pulled text, once


@unittest.skipUnless(IPFS, "kubo (ipfs) is not installed")
class KuboProjectTest(unittest.TestCase):
    """The CLI end to end through one offline kubo repo (it stands for the network): A adds and
    pushes; B pulls the CID and holds the same head id. IPFS_PATH is a temp dir: the user's repo is
    never touched, and no daemon runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, IPFS_PATH=os.path.join(self.tmp.name, "ipfs"), BREADCRUMBS_SUMMARIZE="0")
        subprocess.run([IPFS, "init", "--profile", "test"], env=self.env, check=True, capture_output=True)
        self.home = {n: os.path.join(self.tmp.name, n) for n in "ab"}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, peer, *args):
        p = subprocess.run([sys.executable, "-B", "-m", "breadcrumbs", "--home", self.home[peer], *args],
                           cwd=REPO, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_push_then_pull_by_cid_gives_the_same_head(self):
        a = Peer(self.home["a"])
        x = a.note("the cassette is worn", "x")
        a.close()
        self.cli("a", "project", "p", x)
        pushed = json.loads(self.cli("a", "push", "p"))
        pulled = json.loads(self.cli("b", "pull", "p", pushed["cid"]))
        self.assertEqual(pulled["head"], pushed["head"])
        self.assertTrue(pushed["cid"].startswith("bagu"))
        self.assertIn("cassette", self.cli("b", "find", "cassette worn"))


if __name__ == "__main__":
    unittest.main()
