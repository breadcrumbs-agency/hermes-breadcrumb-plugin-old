"""One memory budget for each user turn. With a turn factor above 0, recall, find and open
share turn_factor x the recall budget. A full turn cuts find to handles and open to the level that
fits, and the result says so. The default (0) changes nothing."""
import json
import os
import tempfile
import unittest

os.environ["BREADCRUMBS_SUMMARIZE"] = "0"  # no summary model in tests

from breadcrumbs.crumbs import get_crumb  # noqa: E402
from breadcrumbs.hermes import BreadcrumbsProvider  # noqa: E402

LONG = "\n".join("Line %d of the long answer: the kettle, the chain, the cog and the compost." % i for i in range(60))


class TurnBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = self.p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        tick = iter(range(1000, 10 ** 6, 10))
        p.clock = lambda: next(tick)
        for i in range(6):
            p.sync_turn("question %d about the kettle and the chain" % i, LONG, session_id="s1")
        p.on_session_switch("s2", reset=True)
        self.big = p.session_crumb("s1")  # about 27 KB at level 1

    def budget(self, factor):
        """Set the turn factor; return the turn budget in bytes (the controller adapts the recall budget)."""
        self.p.turn_factor = factor
        return int(factor * 4 * self.p.ctl.budget)

    def call(self, tool, **args):
        return json.loads(self.p.handle_tool_call(tool, args))["text"]

    def receipts(self, sid="s2"):
        out, rid = [], self.p.refs.get("rcpt-" + sid)
        while rid:
            c = get_crumb(self.p.store, rid)
            out.append(c.meta)
            rid = c.meta.get("prev") or None
        return out[::-1]

    def test_off_by_default(self):
        self.assertEqual(self.call("mcp__crumb", id=self.big, level=1.0), self.p.zoom.render(self.big, 1.0))
        self.assertFalse({"cap", "used", "tb"} & set(self.receipts()[-1]))

    def test_open_cuts_to_the_level_that_fits_the_turn(self):
        tb = self.budget(2)  # 2 x the recall budget, in bytes
        text = self.call("mcp__crumb", id=self.big, level=1.0)
        m = self.receipts()[-1]
        self.assertEqual((m["cap"], m["tb"]), (True, tb))
        self.assertTrue(text.startswith("[cut:"), text[:80])
        self.assertLess(m["lv"][0], 1.0)
        self.assertLessEqual(len(text.encode("utf-8")), tb)
        self.assertEqual(m["used"], len(text.encode("utf-8")))

    def test_find_keeps_the_handles_and_drops_lines_first(self):
        full = self.call("mcp__crumb_find", q="kettle chain cog")
        self.p.on_session_switch("s3", reset=True)
        tb = self.budget(0.5)
        cut = self.call("mcp__crumb_find", q="kettle chain cog")

        def count(text):
            lines = [x for x in text.splitlines() if x.strip() and not x.startswith("[cut:")]
            heads = sum(1 for x in lines if x.lstrip().startswith("\u25b8"))
            return heads, len(lines) - heads
        (h_full, l_full), (h_cut, l_cut) = count(full), count(cut)
        self.assertIn("[cut:", cut)
        self.assertLess(l_cut, l_full)
        self.assertGreater(h_cut, l_cut)
        self.assertLessEqual(len(cut.encode("utf-8")), tb)

    def test_a_cut_find_never_shows_a_line_without_its_hit(self):
        for room in range(170, 1700, 7):  # handles and lines differ in size: some rooms end between them
            out = self.p.find("kettle chain cog", room=room)
            lines = [x for x in out.splitlines() if x.strip() and not x.startswith("[cut:")]
            for prev, line in zip([""] + lines, lines):
                if not line.lstrip().startswith("\u25b8"):
                    self.assertTrue(prev.startswith("  \u25b8"), (room, prev[:40], line[:40]))

    def test_a_turn_with_little_room_gives_the_handle(self):
        tb = self.budget(2)
        self.p._spend(tb - 200)  # more than the note, less than one step of the crumb
        text = self.call("mcp__crumb", id=self.big, level=1.0)
        self.assertIn(self.big, text)
        self.assertEqual(self.receipts()[-1]["lv"], [0.0])

    def test_a_cut_open_records_what_it_asked_for(self):
        self.budget(2)
        full = len(self.p.zoom.render(self.big, 1.0).encode("utf-8"))
        self.call("mcp__crumb", id=self.big, level=1.0)
        m = self.receipts()[-1]
        self.assertEqual((m["cap"], m["want"]), (True, full))
        self.assertLess(m["bytes"], m["want"])

    def test_a_cut_find_records_the_bytes_of_all_its_lines(self):
        full = self.call("mcp__crumb_find", q="kettle chain cog")  # no turn budget
        self.p.on_session_switch("s3", reset=True)
        self.budget(0.5)
        self.call("mcp__crumb_find", q="kettle chain cog")
        m = self.receipts("s3")[-1]
        self.assertEqual((m["cap"], m["want"]), (True, len(full.encode("utf-8"))))

    def test_a_cut_recall_records_the_bytes_it_asked_for(self):
        self.p.clock = lambda: 10 ** 6  # one time for both recalls: the same weights
        full = self.p.prefetch("the kettle and the chain", session_id="s2")  # no turn budget
        self.p.on_session_switch("s3", reset=True)
        self.budget(0.1)
        self.p.prefetch("the kettle and the chain", session_id="s3")
        m = self.receipts("s3")[-1]
        self.assertEqual((m["ev"], m["cap"], m["want"]), ("recall", True, len(full.encode("utf-8"))))
        self.assertLess(m["bytes"], m["want"])

    def test_an_event_that_fits_wants_its_own_bytes(self):
        self.budget(64)
        self.call("mcp__crumb", id=self.big, level=0.05)
        m = self.receipts()[-1]
        self.assertEqual((m["cap"], m["want"]), (False, m["bytes"]))

    def test_recall_and_open_share_the_turn(self):
        tb = self.budget(2)
        recall = self.p.prefetch("the kettle and the chain", session_id="s2")
        self.assertTrue(recall)
        text = self.call("mcp__crumb", id=self.big, level=1.0)
        r, m = self.receipts()[-2:]
        self.assertEqual((r["ev"], m["ev"]), ("recall", "open"))
        self.assertEqual(m["used"], r["bytes"] + len(text.encode("utf-8")))
        self.assertLessEqual(m["used"], tb)

    def test_a_full_turn_still_gives_the_handle(self):
        self.budget(2)
        self.call("mcp__crumb", id=self.big, level=1.0)
        text = self.call("mcp__crumb", id=self.big, level=1.0)
        self.assertIn(self.big, text)  # the handle names the crumb: the source stays known
        self.assertLess(len(text.encode("utf-8")), 400)
        self.assertEqual(self.receipts()[-1]["lv"], [0.0])

    def test_the_next_user_turn_gets_a_new_budget(self):
        self.budget(2)
        self.call("mcp__crumb", id=self.big, level=1.0)
        self.p.sync_turn("one question", "one answer", session_id="s2")
        self.call("mcp__crumb", id=self.big, level=0.05)  # 5% of about 27 KB fits in 2,400 bytes
        m = self.receipts()[-1]
        self.assertEqual((m["turn"], m["cap"]), (1, False))
        self.assertEqual(m["used"], m["bytes"])



class TurnConfigTest(unittest.TestCase):
    """The factor comes from `hermes memory setup` (the provider's own config.json in its store folder);
    the env var BREADCRUMBS_TURN_FACTOR, when set, wins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.pop("BREADCRUMBS_TURN_FACTOR", None)
        self.addCleanup(lambda: os.environ.__setitem__("BREADCRUMBS_TURN_FACTOR", old) if old is not None
                        else os.environ.pop("BREADCRUMBS_TURN_FACTOR", None))
        home = os.environ.get("HERMES_HOME")  # the schema looks up the profile's slot: never a live profile
        os.environ["HERMES_HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__("HERMES_HOME", home) if home is not None
                        else os.environ.pop("HERMES_HOME", None))

    def open(self):
        p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        return p

    def test_the_schema_offers_the_turn_factor(self):
        field = {f["key"]: f for f in BreadcrumbsProvider().get_config_schema()}["turn_factor"]
        self.assertEqual((field["type"], field["default"]), ("number", 0))

    def test_saved_config_sets_the_factor_of_the_next_agent(self):
        self.assertEqual(self.open().turn_factor, 0.0)
        BreadcrumbsProvider().save_config({"turn_factor": 8}, self.tmp.name)
        self.assertEqual(self.open().turn_factor, 8.0)

    def test_the_env_var_wins(self):
        BreadcrumbsProvider().save_config({"turn_factor": 8}, self.tmp.name)
        os.environ["BREADCRUMBS_TURN_FACTOR"] = "1.5"
        self.assertEqual(self.open().turn_factor, 1.5)

if __name__ == "__main__":
    unittest.main()
