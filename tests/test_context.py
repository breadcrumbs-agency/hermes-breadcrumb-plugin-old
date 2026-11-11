"""Context receipts and the views that SpaceGraph draws: the Gantt of context events, the Venn of sets.

One small scenario with fixed times. The view contract fixture (tests/fixtures/view-contract.json)
holds the View output for it; SpaceGraph's suite asserts the same file. To write it again after an
intended change: BREADCRUMBS_WRITE_FIXTURE=1 python -m unittest tests.test_context
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import view
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.ingest import leaves

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "view-contract.json")
GARDEN = [("Plant tomato seedlings in deep pots near a sunny window, two per pot.",
           "Water them every morning, add compost weekly, and stake each stem when it reaches a hand high."),
          ("Yellow leaves on tomato seedlings?", "Usually too much water: let the compost dry between waterings.")]
TURN_FACTOR = 0.75  # s3's turn budget, in recall budgets: the recall fits; the open is cut (to its handle)
CALL = {"id": "call_1", "type": "function", "function": {"name": "mcp__crumb_find", "arguments": '{"q": "bike chain"}'}}


def scenario(home):
    """s0: an unrelated session, so that s1 and s2 share a cue word (one graph edge). s1: two turns.
    s2, turn 0: recall, open, find, then sync with one tool call. s2, turn 1: compress, recall
    again, sync. Returns the provider (still open) and the receipt ids in order."""
    p = BreadcrumbsProvider()
    p.initialize("s0", hermes_home=home, platform="cli", agent_context="primary")
    tick = iter(range(1000, 10 ** 6, 10))
    p.clock = lambda: next(tick)
    for user, answer in GARDEN:
        p.sync_turn(user, answer, session_id="s0")
    p.on_session_switch("s1")
    p.sync_turn("how long should green tea steep in the kettle water", "steep green tea two minutes at 80 C",
                session_id="s1")
    p.sync_turn("my bike chain skips on the small cog", "replace the worn chain and check the cog", session_id="s1")
    p.on_session_switch("s2", reset=True)
    rcpt = []
    p.prefetch("how long should green tea steep?", session_id="s2")
    rcpt.append(p.refs.get("rcpt-s2"))
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("s1"), "level": 0.5})
    rcpt.append(p.refs.get("rcpt-s2"))
    p.handle_tool_call("mcp__crumb_find", {"q": "bike chain"})
    rcpt.append(p.refs.get("rcpt-s2"))
    p.sync_turn("how long should green tea steep?", "Two minutes at 80 C, as you noted before.", session_id="s2",
                messages=[{"role": "user", "content": "how long should green tea steep?"},
                          {"role": "assistant", "content": None, "tool_calls": [CALL]},
                          {"role": "tool", "tool_call_id": "call_1", "name": "mcp__crumb_find", "content": "bike chain hits"},
                          {"role": "assistant", "content": "Two minutes at 80 C, as you noted before."}])
    p.on_pre_compress([])
    rcpt.append(p.refs.get("rcpt-s2"))
    p.prefetch("what about the bike chain on the small cog?", session_id="s2")
    rcpt.append(p.refs.get("rcpt-s2"))
    p.sync_turn("what about the bike chain on the small cog?", "Replace the worn chain.", session_id="s2")
    p.on_session_switch("s3", reset=True)  # s3: one turn with a turn budget; its open is cut
    p.turn_factor = TURN_FACTOR
    p.prefetch("tomato seedlings and compost?", session_id="s3")
    p.handle_tool_call("mcp__crumb", {"id": p.session_crumb("s0"), "level": 1.0})
    p.turn_factor = 0.0
    p.sync_turn("tomato seedlings and compost?", "Water every morning and add compost weekly.", session_id="s3")
    return p, rcpt


class Scenario(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after every View closes (Windows)
        self.p, self.rcpt = scenario(self.tmp.name)
        self.root = os.path.join(self.tmp.name, "breadcrumbs")
        self.r = [get_crumb(self.p.store, r) for r in self.rcpt]

    def tearDown(self):
        self.p.shutdown()


class ReceiptTest(Scenario):
    def test_each_context_event_writes_one_receipt_in_a_chain(self):
        self.assertEqual(len(set(self.rcpt)), 5)
        self.assertEqual([c.kind for c in self.r], ["rcpt"] * 5)
        self.assertEqual([c.meta["ev"] for c in self.r], ["recall", "open", "find", "compress", "recall"])
        self.assertEqual([c.meta["prev"] for c in self.r], [""] + self.rcpt[:-1])
        self.assertEqual([c.meta["turn"] for c in self.r], [0, 0, 0, 1, 1])
        self.assertEqual([c.meta["ovf"] for c in self.r], [False, False, False, True, False])

    def test_items_levels_and_bytes_line_up(self):
        for c in self.r:
            m = c.meta
            self.assertTrue(m["items"])
            self.assertEqual(len(m["items"]), len(m["lv"]))
            self.assertEqual(len(m["items"]), len(m["b"]))
            self.assertLessEqual(sum(m["b"]), m["bytes"])
            self.assertTrue(set(c.kids) <= set(m["items"]))

    def test_an_open_receipt_holds_the_ref_and_the_level_the_model_asked_for(self):
        m = self.r[1].meta
        self.assertEqual((m["items"], m["lv"]), ([self.p.session_crumb("s1")], [0.5]))
        self.assertEqual(m["b"], [len(self.p.zoom.render(self.p.session_crumb("s1"), 0.5).encode())])

    def test_level_one_of_a_receipt_shows_each_item_at_its_level(self):
        out = self.p.zoom.render(self.rcpt[1], 1.0)
        self.assertTrue(out.startswith("\u25b8 %s rcpt" % self.rcpt[1]))
        self.assertIn(self.p.zoom.render(self.p.session_crumb("s1"), 0.5), out)
        self.assertNotIn(self.p.zoom.render(self.p.session_crumb("s1"), 1.0), out)

    def test_a_receipt_replays_the_text_the_model_saw_after_the_store_grows(self):
        """Below level 1, the words of a text come from the index's counts, and the counts change as the store
        grows. A receipt that rendered its items again would show another text than the model saw."""
        turn = get_crumb(self.p.store, self.p.session_crumb("s1")).kids[0]  # below level 1 it shows key words
        saw = json.loads(self.p.handle_tool_call("mcp__crumb", {"id": turn, "level": 0.7}))["text"]
        rid = self.p.refs.get("rcpt-" + self.p.session_id)  # the receipt of that open
        for i in range(40):  # the words of this turn become common in the store
            self.p.sync_turn("steep green tea in the kettle, bike chain on the small cog %d" % i,
                             "steep the tea in the kettle two minutes, replace the worn chain", session_id="g%d" % i)
        self.p.shutdown()
        self.p = BreadcrumbsProvider()  # a restart: the counts are read again
        self.p.initialize("s9", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.assertNotEqual(self.p.zoom.render(turn, 0.7), saw, "the store must drift, or this test proves nothing")
        self.assertIn(saw, self.p.zoom.render(rid, 1.0))

    def test_a_find_receipt_holds_the_handles_and_the_exact_lines(self):
        m = self.r[2].meta
        self.assertIn(self.p.session_crumb("s1"), m["items"])
        lines = [i for i in m["items"] if ":" in i]
        self.assertTrue(lines)
        self.assertTrue(all(lv == 1.0 for i, lv in zip(m["items"], m["lv"]) if ":" in i))

    def test_each_receipt_records_the_controller_band(self):
        self.assertEqual([c.meta["band"] for c in self.r], [list(self.p.ctl.band)] * 5)

    def test_receipts_are_never_search_hits(self):
        rows = self.p.index.db.execute("SELECT count(*) FROM rows WHERE kind = 'rcpt'").fetchone()[0]
        self.assertEqual(rows, 0)

    def test_a_failed_receipt_write_does_not_fail_the_tool(self):
        from breadcrumbs import hermes
        def full(*a, **k):
            raise OSError("no space left on device")
        hermes.put_crumb, keep = full, hermes.put_crumb
        try:
            out = json.loads(self.p.handle_tool_call("mcp__crumb", {"id": self.p.session_crumb("s1"), "level": 0.5}))
        finally:
            hermes.put_crumb = keep
        self.assertEqual(out["text"], self.p.zoom.render(self.p.session_crumb("s1"), 0.5))
        self.assertEqual(self.p.refs.get("rcpt-s2"), self.rcpt[-1])

    def test_a_read_only_agent_writes_no_receipt(self):
        q = BreadcrumbsProvider()
        q.initialize("s9", hermes_home=self.tmp.name, platform="cli", agent_context="subagent")
        try:
            q.prefetch("how long should green tea steep?", session_id="s9")
            q.handle_tool_call("mcp__crumb", {"id": self.p.session_crumb("s1"), "level": 0.5})
            self.assertIsNone(q.refs.get("rcpt-s9"))
        finally:
            q.shutdown()


class ContextTest(Scenario):
    def setUp(self):
        super().setUp()
        self.v = view.View(self.root, clock=lambda: 5000)
        self.addCleanup(self.v.close)

    def test_recall_and_find_events_name_their_query(self):
        got = [(e["ev"], e["q"]) for e in self.v.context("s2")["events"]]
        self.assertEqual(got, [("recall", "how long should green tea steep?"), ("open", ""), ("find", "bike chain"),
                               ("compress", ""), ("recall", "what about the bike chain on the small cog?")])

    def test_each_row_names_the_session_of_its_crumb(self):
        g = self.v.context("s2")
        rows = {r["id"]: r for r in g["rows"]}
        self.assertEqual(rows[self.p.session_crumb("s1")]["session"], "s1")  # the opened session
        self.assertTrue(all(r["session"] == "s2" for r in g["rows"] if r["bars"][0]["ev"] == "own"))
        self.assertLessEqual({r["session"] for r in g["rows"]}, {"s0", "s1", "s2"})

    def test_events_without_a_turn_budget_show_no_cut(self):
        for e in self.v.context("s2")["events"]:
            self.assertEqual((e["turn_budget"], e["cap"], e["want"]), (None, False, e["bytes"]))

    def test_used_is_the_memory_of_the_turn_so_far_with_the_event(self):
        b = [e["bytes"] for e in self.v.context("s2")["events"]]  # recall open find | compress recall
        self.assertEqual([e["used"] for e in self.v.context("s2")["events"]],
                         [b[0], b[0] + b[1], b[0] + b[1] + b[2], 0, b[4]])

    def test_a_turn_budget_session_shows_its_cut(self):
        ev = self.v.context("s3")["events"]
        self.assertEqual([e["ev"] for e in ev], ["recall", "open"])
        tb = ev[0]["turn_budget"]
        self.assertEqual({e["turn_budget"] for e in ev}, {tb})
        self.assertEqual(tb, int(TURN_FACTOR * ev[0]["budget"]))
        self.assertEqual((ev[0]["cap"], ev[1]["cap"]), (False, True))
        self.assertGreater(ev[1]["want"], ev[1]["bytes"])
        self.assertEqual([e["used"] for e in ev], [ev[0]["bytes"], ev[0]["bytes"] + ev[1]["bytes"]])

    def test_each_event_carries_the_band(self):
        self.assertTrue(all(e["band"] == [0.02, 0.05] for e in self.v.context("s2")["events"]))

    def test_graph_and_session_edges_have_one_shape(self):
        g = self.v.graph()
        self.assertTrue(g["edges"])  # s1 and s2 share a cue word
        for e in g["edges"] + self.v.session("s2")["edges"]:
            self.assertLessEqual({"kind", "source", "target", "w"}, set(e))
            self.assertFalse({"a", "b"} & set(e))

    def test_session_nodes_name_their_use_and_never_heat(self):
        nodes = self.v.session("s1")["nodes"]
        self.assertFalse(any("heat" in n for n in nodes))
        self.assertTrue(any(set(n.get("use", ())) == {"accessed", "uses"} for n in nodes))

    def test_venn_at_must_name_an_event(self):
        for at in (-2, -1, 5, 99):
            with self.assertRaises(ValueError):
                self.v.venn("s2", at=at)

    def test_an_unknown_session_raises_key_error_in_every_view(self):
        for f in (self.v.session, self.v.context, self.v.venn):
            with self.assertRaises(KeyError):
                f("nope")

    def test_a_session_without_receipts_has_three_empty_sets(self):
        vn = self.v.venn("s1")
        self.assertEqual(vn["sets"], {n: {"ids": [], "bytes": 0} for n in ("recalled", "found", "opened")})
        self.assertEqual((vn["at"], vn["regions"]), (None, []))

    def test_events_and_turns_of_the_gantt(self):
        g = self.v.context("s2")
        self.assertEqual([e["ev"] for e in g["events"]], ["recall", "open", "find", "compress", "recall"])
        self.assertEqual([e["id"] for e in g["events"]], self.rcpt)
        self.assertEqual([t["turn"] for t in g["turns"]], [0, 1])
        self.assertEqual([get_crumb(self.p.store, t["crumb"]).meta["role"] for t in g["turns"]], ["user", "user"])

    def test_a_recalled_bar_ends_at_the_next_compress_and_a_late_bar_is_live(self):
        rows = {r["id"]: r for r in self.v.context("s2")["rows"]}
        first = self.r[0].meta["items"][0]
        bar = rows[first]["bars"][0]
        self.assertEqual((bar["from"], bar["to"], bar["t0"], bar["t1"], bar["ev"]), (0, 3, 0, 1, "recall"))
        last = self.r[4].meta["items"][0]
        self.assertIsNone([b for b in rows[last]["bars"] if b["from"] == 4][0]["to"])

    def test_own_crumbs_are_rows_from_their_turn_to_the_next_compress(self):
        own = [r for r in self.v.context("s2")["rows"] if r["bars"][0]["ev"] == "own"]
        kinds = [r["kind"] for r in own]
        self.assertEqual(kinds, ["turn", "step", "tool", "turn", "turn", "turn"])
        self.assertEqual([r["bars"][0]["t1"] for r in own], [1, 1, 1, 1, None, None])
        self.assertTrue(all(r["bars"][0]["level"] == 1.0 and r["bars"][0]["bytes"] > 0 for r in own))

    def test_the_venn_regions_split_the_union_exactly_with_bytes(self):
        vn = self.v.venn("s2", at=2)
        sets = {k: set(s["ids"]) for k, s in vn["sets"].items()}
        self.assertEqual(sorted(sets), ["found", "opened", "recalled"])
        union = set().union(*sets.values())
        seen = []
        for reg in vn["regions"]:
            for i in reg["ids"]:
                self.assertEqual(sorted(k for k, s in sets.items() if i in s), sorted(reg["in"]))
            seen += reg["ids"]
        self.assertEqual(sorted(seen), sorted(union))
        self.assertEqual(sum(r["bytes"] for r in vn["regions"]), sum(s["bytes"] for s in vn["sets"].values()))
        self.assertIn(self.p.session_crumb("s1"), sets["opened"] & sets["found"])

    def test_each_region_splits_its_bytes_by_set(self):
        vn = self.v.venn("s2", at=2)
        for reg in vn["regions"]:
            self.assertEqual(sorted(reg["by"]), sorted(reg["in"]))
            self.assertEqual(sum(reg["by"].values()), reg["bytes"])
        for name, s in vn["sets"].items():
            self.assertEqual(sum(r["by"].get(name, 0) for r in vn["regions"]), s["bytes"])

    def test_after_a_compress_the_venn_starts_again(self):
        vn = self.v.venn("s2")
        self.assertEqual(set(vn["sets"]["recalled"]["ids"]), set(self.r[4].meta["items"]))
        self.assertEqual(vn["sets"]["opened"]["ids"], [])


class TextRowTest(unittest.TestCase):
    def test_a_text_row_names_the_session_of_the_crumb_that_holds_it(self):
        with tempfile.TemporaryDirectory() as home:
            p = BreadcrumbsProvider()
            p.initialize("s1", hermes_home=home, platform="cli", agent_context="primary")
            p.sync_turn("my bike chain skips on the small cog", "replace the worn chain", session_id="s1")
            doc = get_crumb(p.store, leaves(p.store, p.session_crumb("s1"))[0]).ranges[0][0]
            p.on_session_switch("s2", reset=True)
            p.handle_tool_call("mcp__crumb", {"id": doc + ":0-1", "level": 1.0})
            p.shutdown()
            v = view.View(os.path.join(home, "breadcrumbs"))
            try:
                rows = {r["id"]: r["session"] for r in v.context("s2")["rows"]}
            finally:
                v.close()
        self.assertEqual(rows, {doc + ":0-1": "s1"})


class SameSecondTest(unittest.TestCase):
    def test_an_own_bar_ends_at_a_compress_in_the_same_second(self):
        with tempfile.TemporaryDirectory() as home:
            p = BreadcrumbsProvider()
            p.initialize("s1", hermes_home=home, platform="cli", agent_context="primary")
            p.clock = lambda: 1000  # every write in one second
            p.sync_turn("my bike chain skips on the small cog", "replace the worn chain", session_id="s1")
            p.on_session_switch("s2", reset=True)
            p.prefetch("bike chain?", session_id="s2")
            p.sync_turn("bike chain?", "Replace the worn chain.", session_id="s2")
            p.on_pre_compress([])
            p.shutdown()
            v = view.View(os.path.join(home, "breadcrumbs"))
            try:
                g = v.context("s2")
            finally:
                v.close()
        stop = [e["ev"] for e in g["events"]].index("compress")
        own = [r["bars"][0] for r in g["rows"] if r["bars"][0]["ev"] == "own"]
        self.assertTrue(own)
        self.assertTrue(all((b["to"], b["t1"]) == (stop, 1) for b in own), own)


class ContractTest(unittest.TestCase):
    def test_the_view_output_matches_the_contract_fixture(self):
        with tempfile.TemporaryDirectory() as home:
            p, _ = scenario(home)
            p.shutdown()
            v = view.View(os.path.join(home, "breadcrumbs"), clock=lambda: 5000)
            try:
                got = json.loads(json.dumps({"graph": v.graph(), "session": v.session("s2"),
                                             "session_s1": v.session("s1"), "context": v.context("s2"),
                                             "context_s3": v.context("s3"),
                                             "venn": v.venn("s2", at=2)}, sort_keys=True))
            finally:
                v.close()
        if os.environ.get("BREADCRUMBS_WRITE_FIXTURE"):
            os.makedirs(os.path.dirname(FIXTURE), exist_ok=True)
            with open(FIXTURE, "w", encoding="utf-8") as f:
                json.dump(got, f, indent=1, sort_keys=True, ensure_ascii=False)
                f.write("\n")
        with open(FIXTURE, encoding="utf-8") as f:
            self.assertEqual(got, json.load(f))


if __name__ == "__main__":
    unittest.main()
