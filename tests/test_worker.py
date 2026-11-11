"""The worker receipt: on_delegation records a delegation, on the parent's side, as one "rcpt" crumb of the event
"worker": the brief that the lead gave the child, the report that came back, the refs that each of them names, and
the session ids. Hermes calls on_delegation once for each child, before the tool returns."""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import hermes, view
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.ingest import leaves
from breadcrumbs.slot import Slot

BRIEF = "The user said (%s): where is the kettle? Read %s and %s first, then find it."
REPORT = "It is on the top shelf, see %s. The date 20261110 and the hash deadbeef open nothing."


class FakeSlot:
    """A memory provider on top of breadcrumbs. It records its on_delegation calls, and raises when told to."""
    name = "fake"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def on_delegation(self, task, result, *, child_session_id=""):
        self.calls.append((task, result, child_session_id))
        if self.fail:
            raise RuntimeError("slot down")


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after every provider and View closes (Windows)
        self.p = self.make("primary")
        tick = iter(range(1000, 10 ** 6, 10))
        self.p.clock = lambda: next(tick)
        self.p.sync_turn("where is the kettle?", "on the top shelf", session_id="s0")  # an earlier session
        self.p.on_session_switch("s1", reset=True)  # s1 is the lead: the session that delegates
        self.old = self.p.session_crumb("s0")
        self.turn = leaves(self.p.store, self.old)[0]
        doc, a, b = get_crumb(self.p.store, self.turn).ranges[0]
        self.span = "%s:%d-%d" % (doc, a, b)  # lines of a text, as the handbook names them

    def make(self, ctx):
        p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context=ctx)
        self.addCleanup(p.shutdown)
        return p

    def chain(self, sid="s1"):
        """The receipts of a session, oldest first: [(id, crumb)]."""
        out, rid = [], self.p.refs.get("rcpt-" + sid)
        while rid:
            c = get_crumb(self.p.store, rid)
            out.append((rid, c))
            rid = c.meta.get("prev") or None
        return out[::-1]

    def test_one_delegation_gives_one_worker_receipt_with_the_brief_and_the_report(self):
        brief, report = "find the kettle in the notes", "it is on the top shelf"
        self.p.on_delegation(brief, report, child_session_id="c1")
        ((rid, c),) = self.chain()
        self.assertEqual((c.kind, c.meta["ev"], c.meta["sess"], c.meta["child"]), ("rcpt", "worker", "s1", "c1"))
        text = self.p.zoom.render(rid, 1.0)
        self.assertTrue(text.startswith("\u25b8 %s rcpt" % rid))
        for part in (brief, report, "parent: s1", "child: c1"):
            self.assertIn(part, text)
        self.assertLess(text.index(brief), text.index(report))

    def test_the_refs_that_open_in_the_store_are_named_and_cited(self):
        brief, report = BRIEF % (self.old, self.turn, self.span), REPORT % self.turn
        self.p.on_delegation(brief + " Not 00000000, nor %s:0-2." % self.old, report, child_session_id="c1")
        ((rid, c),) = self.chain()
        self.assertEqual((c.meta["named"], c.meta["cited"]), ([self.old, self.turn, self.span], [self.turn]))
        self.assertEqual(c.meta["items"], [self.old, self.turn, self.span])
        self.assertEqual(c.kids, [self.old, self.turn])  # the receipt links the crumbs; a span is a range
        self.assertIn(self.span.split(":")[0], [r[0] for r in c.ranges])
        text = self.p.zoom.render(rid, 1.0)  # the text keeps every word as written, the lookalikes too
        for part in (brief, "Not 00000000, nor %s:0-2." % self.old, report):
            self.assertIn(part, text)

    def test_the_car_and_the_root_id_are_kept_once_the_lead_holds_them(self):
        rid = self.p._worker_receipt("find the kettle", "found it", "c1", car="bafycar", root=self.turn)
        c = get_crumb(self.p.store, rid)
        self.assertEqual((c.meta["car"], c.meta["root"]), ("bafycar", self.turn))
        text = self.p.zoom.render(rid, 1.0)
        self.assertIn("car: bafycar\nroot: %s\n" % self.turn, text)

    def test_a_read_only_agent_writes_no_receipt(self):
        q = self.make("subagent")
        q.on_delegation("find the kettle", "found it", child_session_id="c1")
        self.assertIsNone(q.refs.get("rcpt-s1"))

    def test_a_slot_still_gets_its_call(self):
        for fail in (False, True):  # a slot that fails does not stop the receipt either
            with self.subTest(fail=fail):
                fake = FakeSlot(fail)
                self.p.slot = Slot("fake", fake, self.p._log)
                before = len(self.chain())
                self.p.on_delegation("find the kettle", "found it", child_session_id="c1")
                self.assertEqual(fake.calls, [("find the kettle", "found it", "c1")])
                self.assertEqual(len(self.chain()), before + 1)

    def test_a_failed_write_does_not_fail_the_delegation(self):
        with mock.patch.object(hermes, "put_crumb", side_effect=ValueError("boom")):
            self.p.on_delegation("find the kettle", "found it", child_session_id="c1")
        self.assertIsNone(self.p.refs.get("rcpt-s1"))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "provider.log"), encoding="utf-8") as f:
            self.assertIn("worker receipt failed", f.read())

    def test_the_gantt_counts_no_bytes_for_a_worker_and_draws_no_bar(self):
        p = self.p
        p.prefetch("where is the kettle?", session_id="s1")
        p.on_delegation(BRIEF % (self.old, self.turn, self.span), REPORT % self.turn, child_session_id="c1")
        p.handle_tool_call("mcp__crumb", {"id": self.old, "level": 0.5})
        v = view.View(os.path.join(self.tmp.name, "breadcrumbs"), clock=lambda: 5000)
        self.addCleanup(v.close)
        g = v.context("s1")
        recall, worker, opened = g["events"]
        self.assertEqual((recall["ev"], worker["ev"], opened["ev"]), ("recall", "worker", "open"))
        self.assertEqual(opened["used"], recall["bytes"] + opened["bytes"])  # the worker text is not in the context
        self.assertEqual(worker["q"], BRIEF % (self.old, self.turn, self.span))
        self.assertNotIn("worker", {b["ev"] for r in g["rows"] for b in r["bars"]})
        self.assertEqual(v.venn("s1")["at"], 2)


if __name__ == "__main__":
    unittest.main()
