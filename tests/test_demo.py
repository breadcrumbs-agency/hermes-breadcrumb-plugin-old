"""The demo (demo/seed.py) makes a store that the explorer can show: a map with cue edges, a Gantt of
context events with one turn cut by a turn budget, and a Venn with all three sets. No model and no network."""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest

from breadcrumbs import view
from breadcrumbs.hermes import _ref_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def seed_module():
    spec = importlib.util.spec_from_file_location("demo_seed", os.path.join(ROOT, "demo", "seed.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DemoTest(unittest.TestCase):
    def test_the_demo_store_has_a_map_a_gantt_and_a_venn(self):
        seed = seed_module()
        with tempfile.TemporaryDirectory() as home:
            with contextlib.redirect_stdout(io.StringIO()):
                seed.main(home)
            v = view.View(os.path.join(home, "breadcrumbs"))
            try:
                g, ctx = v.graph(), v.context(seed.NOW)
                self.assertGreaterEqual(len(g["nodes"]), 8)
                sess = {v.refs.get(_ref_name("sess-", sid)): sid for sid in seed.PAST}
                pairs = {frozenset(sess.get(x, x) for x in (e["source"], e["target"])) for e in g["edges"]}
                self.assertLessEqual({frozenset(("tea", "tea-2")), frozenset(("bike", "bike-2"))}, pairs,
                                     "two sessions on one topic share a cue word")
                self.assertEqual([e["ev"] for e in ctx["events"]],
                                 ["recall", "open", "recall", "find", "open", "compress", "recall", "open", "recall",
                                  "recall", "open", "open"])
                full = [n for n, s in v.venn(seed.NOW, at=4)["sets"].items() if s["ids"]]
                self.assertEqual(sorted(full), ["found", "opened", "recalled"])
                ev = ctx["events"]  # the last turn runs under a turn budget: it fills, and its last open is cut
                cut = [e for e in ev if e["turn"] == ev[-1]["turn"]]
                self.assertEqual({e["turn_budget"] for e in ev if e["turn"] != ev[-1]["turn"]}, {None})
                self.assertEqual([(e["ev"], e["cap"]) for e in cut], [("recall", False), ("open", False), ("open", True)])
                self.assertEqual({e["turn_budget"] for e in cut}, {cut[0]["turn_budget"]})
                self.assertGreater(cut[2]["want"], cut[2]["bytes"])
                self.assertGreater(cut[2]["used"], cut[1]["used"])
            finally:
                v.close()


if __name__ == "__main__":
    unittest.main()
