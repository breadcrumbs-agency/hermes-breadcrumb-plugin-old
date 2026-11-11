"""Supersede tags: a separate crumb says that crumb OLD is superseded by crumb NEW; a neg tag retracts it.
The commits of the ref "tags" keep the order; the index keeps the current state."""
import contextlib
import io
import json
import os
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import ace, cli, tags
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.history import Refs, log
from breadcrumbs.index import Index
from breadcrumbs.ingest import put_turn
from breadcrumbs.store import Store
from test_ace import chat

OLD = "The kettle timer for green tea is four minutes"
NEW = "The kettle timer for green tea is two minutes"
THIRD = "The kettle timer for green tea is three minutes"


class TagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.index = Index(os.path.join(self.tmp.name, "index.db"))
        self.refs = Refs(os.path.join(self.tmp.name, "refs"))
        self.old = put_turn(self.store, self.index, "user", OLD, "s0", ts=1000)
        self.new = put_turn(self.store, self.index, "user", NEW, "s1", ts=2000)

    def tearDown(self):
        self.index.close()
        self.store.close()

    def supersede(self, old, new, undo=False):
        return tags.supersede(self.store, self.index, self.refs, old, new, undo=undo)

    def test_a_tag_is_a_separate_private_crumb_that_names_the_old_crumb_then_the_new_one(self):
        before = (self.store.get(self.old), self.store.get(self.new))
        tag = self.supersede(self.old, self.new)
        c = get_crumb(self.store, tag)
        self.assertEqual((c.kind, c.title, c.heritage, c.public, c.meta, c.ranges, c.kids),
                         ("tag", "superseded", [self.old, self.new], False, {}, [], []))
        self.assertEqual((self.store.get(self.old), self.store.get(self.new)), before)  # the judged crumbs stay
        self.assertEqual(tags.successors(self.index, [self.old, self.new]), {self.old: self.new})
        self.assertEqual([c.kids for _, c in log(self.store, self.refs.get("tags"))], [[tag]])

    def test_undo_writes_a_neg_tag_and_the_same_tag_again_is_the_same_crumb(self):
        tag = self.supersede(self.old, self.new)
        neg = self.supersede(self.old, self.new, undo=True)
        self.assertNotEqual(neg, tag)
        c = get_crumb(self.store, neg)
        self.assertEqual((c.kind, c.title, c.heritage, c.meta), ("tag", "superseded", [self.old, self.new], {"neg": 1}))
        self.assertEqual(tags.successors(self.index, [self.old]), {})
        self.assertEqual(self.supersede(self.old, self.new), tag)
        self.assertEqual(tags.successors(self.index, [self.old]), {self.old: self.new})
        self.assertEqual(len(list(log(self.store, self.refs.get("tags")))), 3)  # three writes, in order

    def test_undo_of_a_tag_that_is_not_there_fails(self):
        with self.assertRaises(ValueError):
            self.supersede(self.old, self.new, undo=True)
        self.assertIsNone(self.refs.get("tags"))

    def test_a_chain_gives_the_newest_crumb_and_a_cycle_is_refused(self):
        third = put_turn(self.store, self.index, "user", THIRD, "s2", ts=3000)
        self.supersede(self.old, self.new)
        self.supersede(self.new, third)
        self.assertEqual(tags.successors(self.index, [self.old, self.new, third]),
                         {self.old: third, self.new: third})
        for old, new in ((third, self.old), (self.old, self.old)):
            with self.assertRaises(ValueError):
                self.supersede(old, new)

    def test_the_text_of_a_superseded_crumb_has_the_same_tag(self):
        self.supersede(self.old, self.new)
        doc, a, b = get_crumb(self.store, self.old).ranges[0]
        ref = "%s:%d-%d" % (doc, a, b)  # the ref of the same text in an ACE layer
        self.assertEqual(tags.successors(self.index, [ref]), {ref: self.new})

    def test_a_text_ref_names_the_crumb_that_holds_it_and_an_unknown_id_fails(self):
        doc, a, b = get_crumb(self.store, self.old).ranges[0]
        tag = self.supersede("%s:%d-%d" % (doc, a, b), self.new)
        self.assertEqual(get_crumb(self.store, tag).heritage, [self.old, self.new])
        for bad in ("deadbeef", "not-an-id"):
            with self.assertRaises(KeyError):
                self.supersede(bad, self.new)

    def test_replay_makes_the_index_state_again_from_the_commits(self):
        third = put_turn(self.store, self.index, "user", THIRD, "s2", ts=3000)
        self.supersede(self.old, self.new)
        self.supersede(self.old, self.new, undo=True)
        self.supersede(self.old, self.new)
        self.supersede(self.new, third)
        self.supersede(self.new, third, undo=True)
        want = tags.successors(self.index, [self.old, self.new, third])
        self.assertEqual(want, {self.old: self.new})
        self.index.db.execute("DELETE FROM tags")
        self.assertEqual(tags.successors(self.index, [self.old]), {})
        tags.replay(self.store, self.index, self.refs)
        self.assertEqual(tags.successors(self.index, [self.old, self.new, third]), want)

    def test_holders_find_the_crumbs_of_an_exact_text_and_write_nothing(self):
        self.store.flush()
        n = len(self.store)
        self.assertEqual(tags.holders(self.store, self.index, OLD), [self.old])
        self.assertEqual(tags.holders(self.store, self.index, OLD, kind="mem"), [])
        self.assertEqual(tags.holders(self.store, self.index, OLD + " or five"), [])
        self.assertEqual(tags.holders(self.store, self.index, "a text that the store never saw"), [])
        self.assertEqual(len(self.store), n)

    def test_an_index_from_before_tags_reads_as_no_tags(self):
        self.supersede(self.old, self.new)
        self.index.db.execute("DROP TABLE tags")
        self.index.commit()
        ro = Index(os.path.join(self.tmp.name, "index.db"), readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(tags.successors(ro, [self.old]), {})
        self.assertEqual(tags.successors(None, [self.old]), {})


class FindTest(unittest.TestCase):
    def test_find_gives_the_id_of_stored_bytes_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with Store(os.path.join(d, "store")) as s:
                a = s.put(b"one line\n")
                self.assertEqual(s.find(b"one line\n"), a)  # in the open block
                s.flush()
                self.assertEqual(s.find(b"one line\n"), a)
                self.assertIsNone(s.find(b"another line\n"))
                self.assertEqual(len(s), 1)


class ProviderTest(unittest.TestCase):
    """Real labels: Hermes replaces a memory entry, and recall shows the old entry as a handle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(self.p.shutdown)

    def mems(self, text):
        return tags.holders(self.p.store, self.p.index, text, kind="mem")

    def replace(self, old, new):
        self.p.on_memory_write("replace", "user", new, {"previous_content": old})

    def test_a_replaced_memory_entry_is_tagged_as_superseded_by_the_new_entry(self):
        self.p.on_memory_write("add", "user", OLD)
        self.replace(OLD, NEW)
        (old,), (new,) = self.mems(OLD), self.mems(NEW)
        self.assertEqual(tags.successors(self.p.index, [old]), {old: new})
        self.assertEqual(get_crumb(self.p.store, new).meta["action"], "replace")  # the mem crumb stays as it was
        self.assertEqual(len(list(log(self.p.store, self.p.refs.get("memory")))), 2)

    def test_add_remove_and_a_replace_of_an_unknown_entry_write_no_tag(self):
        self.p.on_memory_write("add", "user", OLD)
        self.p.on_memory_write("remove", "user", "", {"previous_content": OLD})
        self.replace(THIRD, NEW)  # THIRD has no crumb, and the replace does not store it
        self.assertIsNone(self.p.refs.get("tags"))
        self.assertEqual(self.mems(THIRD), [])

    def test_a_value_that_comes_back_is_current_again(self):
        self.p.on_memory_write("add", "user", OLD)
        self.replace(OLD, NEW)
        self.replace(NEW, OLD)
        first, back = self.mems(OLD)
        doc, a, b = get_crumb(self.p.store, back).ranges[0]
        self.assertEqual(tags.successors(self.p.index, [first, back, "%s:%d-%d" % (doc, a, b)]), {first: back})

    def test_recall_shows_a_superseded_crumb_as_a_handle_next_to_its_successor(self):
        self.p.on_memory_write("add", "user", OLD)
        self.replace(OLD, NEW)
        (old,), (new,) = self.mems(OLD), self.mems(NEW)
        self.p.on_session_switch("s2", reset=True)
        out = self.p.prefetch("how long is the kettle timer for green tea?", session_id="s2")
        self.assertIn(NEW, out)
        self.assertNotIn("four", out)
        at = out.index("\u25b8 %s mem \u00b7" % old)
        self.assertIn("superseded by %s\n" % new, out[at:])
        self.assertNotIn("\n\u25b8 ", out[out.index(NEW):at - 1])  # right after its successor
        text = json.loads(self.p.handle_tool_call("crumb", {"id": old, "level": 1.0}))["text"]
        self.assertIn(OLD, text)  # crumb(OLD, 1) gives the exact old text

    def test_recall_brings_the_successor_when_only_the_old_crumb_matches(self):
        self.p.on_memory_write("add", "user", "The walrus kettle is on shelf four")
        self.replace("The walrus kettle is on shelf four", "Moved it: now in the garage")
        (old,) = self.mems("The walrus kettle is on shelf four")
        self.p.on_session_switch("s2", reset=True)
        out = self.p.prefetch("where is the walrus kettle?", session_id="s2")
        self.assertIn("Moved it: now in the garage", out)
        self.assertIn("\u25b8 %s mem" % old, out)
        self.assertNotIn("shelf four", out)

    def test_without_a_tag_recall_shows_both_texts(self):
        self.p.on_memory_write("add", "user", OLD)
        self.p.on_memory_write("add", "user", NEW)
        self.p.on_session_switch("s2", reset=True)
        out = self.p.prefetch("how long is the kettle timer for green tea?", session_id="s2")
        self.assertIn(OLD, out)
        self.assertIn(NEW, out)
        self.assertNotIn("superseded", out)

    def test_a_subagent_sees_the_handle_and_writes_nothing(self):
        self.p.on_memory_write("add", "user", OLD)
        self.replace(OLD, NEW)
        (old,) = self.mems(OLD)
        sub = BreadcrumbsProvider()
        sub.initialize("s9", hermes_home=self.tmp.name, platform="cli", agent_context="subagent")
        self.addCleanup(sub.shutdown)
        before = len(sub.store)
        out = sub.prefetch("how long is the kettle timer for green tea?", session_id="s9")
        self.assertIn(NEW, out)
        self.assertIn("\u25b8 %s mem" % old, out)
        self.assertEqual(len(sub.store), before)


class AceTest(unittest.TestCase):
    def test_a_superseded_text_in_the_layer_is_a_handle_that_names_its_successor(self):
        with tempfile.TemporaryDirectory() as home:
            p = BreadcrumbsProvider()
            p.initialize("s0", hermes_home=home, platform="cli", agent_context="primary")
            self.addCleanup(p.shutdown)
            p.on_memory_write("add", "user", OLD)
            p.on_memory_write("add", "user", NEW)
            (old,), (new,) = (tags.holders(p.store, p.index, t, kind="mem") for t in (OLD, NEW))
            req = [{"role": "system", "content": "You are a test agent."},
                   {"role": "user", "content": OLD}, {"role": "assistant", "content": "noted"}] + chat(40)
            layers = []
            for tagged in (False, True):
                if tagged:
                    tags.supersede(p.store, p.index, p.refs, old, new)
                e = ace.Ace(target_tokens=2 * ace.SMALL_TARGET)
                e.on_session_start("s1", hermes_home=home)
                layers.append(e.select_context(req, budget_tokens=1_000_000)[1]["content"])
                ref = ace.ref_of(e.store, OLD)
                self.assertEqual(e.zoom.render(ref, 1.0), OLD)  # the exact text stays one call away
                e.close()
            self.assertIn(OLD, layers[0])
            self.assertNotIn("four", layers[1])
            line = next(x for x in layers[1].splitlines() if x.startswith("\u25b8 %s " % ref))
            self.assertTrue(line.endswith(" superseded by %s" % new), line)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        store, index = Store(os.path.join(self.home, "store")), Index(os.path.join(self.home, "index.db"))
        self.old = put_turn(store, index, "user", OLD, "s0", ts=1000)
        self.new = put_turn(store, index, "user", NEW, "s1", ts=2000)
        store.flush()
        index.commit()
        index.close()
        store.close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", self.home, *argv])
        return code, out.getvalue(), err.getvalue()

    def state(self):
        index = Index(os.path.join(self.home, "index.db"), readonly=True)
        try:
            return tags.successors(index, [self.old])
        finally:
            index.close()

    def test_supersede_and_undo(self):
        code, out, _ = self.run_cli("supersede", self.old, self.new)
        self.assertEqual(code, 0)
        got = json.loads(out)
        self.assertEqual((got["old"], got["new"], got["undo"]), (self.old, self.new, False))
        self.assertEqual(self.state(), {self.old: self.new})
        code, out, _ = self.run_cli("supersede", self.old, self.new, "--undo")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["undo"])
        self.assertEqual(self.state(), {})

    def test_an_unknown_id_or_a_missing_tag_fails_with_a_message(self):
        code, _, err = self.run_cli("supersede", "deadbeef", self.new)
        self.assertEqual(code, 1)
        self.assertIn("deadbeef", err)
        code, _, err = self.run_cli("supersede", self.old, self.new, "--undo")
        self.assertEqual(code, 1)
        self.assertIn("not superseded", err)


if __name__ == "__main__":
    unittest.main()
