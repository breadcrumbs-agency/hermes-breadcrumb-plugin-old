"""The dial `family`, second half (breadcrumbs/handles.py and BreadcrumbsProvider.on_delegation).

At on_delegation the CAR of a delegate_task child comes into the lead's own store as private crumbs, so the lead opens the
exact text of the child by id. The hook transform_tool_result then adds one handle line for each child to the result of
the delegate_task call, in the entry of that child, so the model learns the id. With the dial off nothing of this runs.

The fixture state.db is the one of test_family: a parent p1 and its children c1, c2 and c3 (c3 keeps no turn)."""
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import handles
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider, register_handles
from breadcrumbs.share import export_car, read_car
from breadcrumbs.slot import Slot
from test_family import make_state_db

KETTLE, SHELF = "measure the kettle", "check the shelf"  # the goals of c1 and c2 in the fixture
LINE = "\u25b8"


class FakeSlot:
    """A memory provider on top of breadcrumbs, that answers nothing."""
    name = "fake"

    def on_delegation(self, task, result, *, child_session_id=""):
        pass


class FamilyHome(unittest.TestCase):
    """One lead p1 over the fixture state.db. CFG and CONTEXT say how its provider starts."""
    CFG, CONTEXT = {"family": True}, "primary"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after the provider closes (Windows)
        self.home = self.tmp.name
        self.root = os.path.join(self.home, "breadcrumbs")
        make_state_db(os.path.join(self.home, "state.db"))
        handles._offers.clear()  # the offers are state of the process: no test leaves one for another
        self.addCleanup(handles._offers.clear)
        os.makedirs(self.root)
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(self.CFG, f)
        self.p = BreadcrumbsProvider()
        self.p.initialize("p1", hermes_home=self.home, platform="cli", agent_context=self.CONTEXT)
        self.addCleanup(self.p.shutdown)

    def log(self):
        with open(os.path.join(self.root, "provider.log"), encoding="utf-8") as f:
            return f.read()

    def receipts(self, sid="p1"):
        """The receipts of a session, oldest first: [(id, crumb)]."""
        out, rid = [], self.p.refs.get("rcpt-" + sid)
        while rid:
            c = get_crumb(self.p.store, rid)
            out.append((rid, c))
            rid = c.meta.get("prev") or None
        return out[::-1]

    def open(self, ref, level=1.0):
        return json.loads(self.p.handle_tool_call("mcp__crumb", {"id": ref, "level": level}))["text"]

    def delegate(self, goal, sid, report="a summary"):
        self.p.on_delegation(goal, report, child_session_id=sid)

    def result(self, *entries):
        """The JSON text that delegate_task returns: one entry for each task, in task order."""
        return json.dumps({"results": [dict({"task_index": i, "status": "completed", "summary": "s%d" % i, "api_calls": 2}, **e)
                                       for i, e in enumerate(entries)], "total_duration_seconds": 1.2}, ensure_ascii=False)

    def hook(self, goals, text, session="p1", ms=1200, tool="delegate_task"):
        args = {"tasks": [{"goal": g} for g in goals]}
        return handles.transform_tool_result(tool_name=tool, args=args, result=text, session_id=session, duration_ms=ms)


class ImportTest(FamilyHome):
    def test_the_parent_opens_the_exact_text_of_a_child_by_id(self):
        self.delegate(KETTLE, "c1", "The kettle is at eighty degrees.")
        ((rid, c),) = self.receipts()
        root = c.meta["root"]
        shown = self.open(root)
        self.assertTrue(shown.startswith("%s %s sess" % (LINE, root)), shown)
        for words in ("measure the kettle", "kettle at eighty degrees", "The kettle is at eighty degrees."):
            self.assertIn(words, shown)  # the user turn, the tool result and the answer, word for word
        self.assertNotIn("oak", shown)  # nothing of c2
        self.assertNotIn("split the job", shown)  # nothing of the parent

    def test_the_worker_receipt_holds_the_cid_of_the_car_and_the_root_id(self):
        self.delegate(KETTLE, "c1")
        ((rid, c),) = self.receipts()
        car = os.path.join(self.root, "children", "c1", "share", "c1.car")
        self.assertEqual(c.meta["car"], read_car(car)[0][0])  # the CID of the file that was imported
        self.assertEqual(c.meta["child"], "c1")
        self.assertIn("car: %s\nroot: %s\n" % (c.meta["car"], c.meta["root"]), self.open(rid))

    def test_the_imported_crumbs_are_private_and_no_search_or_export_of_a_peer_names_them(self):
        self.delegate(KETTLE, "c1")
        root = self.receipts()[0][1].meta["root"]
        seen, stack = [], [root]
        while stack:
            crumb_id = stack.pop()
            c = get_crumb(self.p.store, crumb_id)
            self.assertFalse(c.public, "%s %s" % (crumb_id, c.kind))
            seen.append(crumb_id)
            stack.extend(c.kids)
        self.assertGreaterEqual(len(seen), 5)  # the session, its turns, a step and a tool
        with self.assertRaises(ValueError):  # an export names public crumbs only
            export_car(self.p.store, [root], os.path.join(self.home, "again.car"))
        self.assertTrue(self.p.index.search("kettle eighty degrees", 8))  # the local search finds them
        self.assertEqual(self.p.index.search("kettle eighty degrees", 8, public_only=True), [])  # a hive peer does not

    def test_crumb_find_finds_the_work_of_the_child_at_once(self):
        self.delegate(SHELF, "c2", "The shelf is made of oak.")
        found = json.loads(self.p.handle_tool_call("mcp__crumb_find", {"q": "shelf oak"}))["text"]
        self.assertIn("The shelf is made of oak.", found)

    def test_the_same_child_again_comes_in_with_the_same_ids(self):
        self.delegate(KETTLE, "c1")
        self.delegate(KETTLE, "c1")
        (_, first), (_, second) = self.receipts()
        self.assertEqual((first.meta["root"], first.meta["car"]), (second.meta["root"], second.meta["car"]))

    def test_each_child_has_its_own_root(self):
        self.delegate(KETTLE, "c1")
        self.delegate(SHELF, "c2")
        (_, first), (_, second) = self.receipts()
        self.assertNotEqual(first.meta["root"], second.meta["root"])
        self.assertIn("eighty degrees", self.open(first.meta["root"]))
        self.assertIn("oak", self.open(second.meta["root"]))

    def test_a_failed_import_is_logged_and_the_delegation_goes_on(self):
        with mock.patch("breadcrumbs.share.import_car", side_effect=ValueError("boom")):
            self.delegate(KETTLE, "c1")
        ((rid, c),) = self.receipts()
        self.assertIn("family: child c1: import failed: ValueError: boom", self.log())
        self.assertNotIn("root", c.meta)  # the work did not come in: the receipt says only what is true
        self.assertEqual(c.meta["car"], read_car(os.path.join(self.root, "children", "c1", "share", "c1.car"))[0][0])
        self.assertIsNone(self.hook([KETTLE], self.result({})))  # and no line goes to the model

    def test_a_child_that_keeps_no_turn_leaves_a_receipt_and_no_root(self):
        self.delegate("a task with no rows", "c3")
        ((rid, c),) = self.receipts()
        self.assertEqual((c.meta["child"], "car" in c.meta, "root" in c.meta), ("c3", False, False))
        self.assertFalse(os.path.exists(os.path.join(self.root, "children")))


class DialOffTest(FamilyHome):
    CFG = {"family": False}

    def test_nothing_comes_in_and_no_line_is_offered(self):
        self.delegate(KETTLE, "c1")
        ((rid, c),) = self.receipts()  # the worker receipt (H10) is not the dial's
        self.assertEqual(("car" in c.meta, "root" in c.meta), (False, False))
        self.assertFalse(os.path.exists(os.path.join(self.root, "children")))
        self.assertEqual(len(handles._offers), 0)
        self.assertIsNone(self.hook([KETTLE], self.result({})))


class ReadOnlyTest(FamilyHome):
    CONTEXT = "subagent"

    def test_a_read_only_agent_brings_in_nothing_and_offers_nothing(self):
        self.assertTrue(self.p.family)
        self.assertFalse(self.p.writable)
        self.delegate(KETTLE, "c1")
        self.assertFalse(os.path.exists(os.path.join(self.root, "children")))
        self.assertEqual(len(handles._offers), 0)


class HandleLineTest(FamilyHome):
    def line_of(self, entry):
        return entry[handles.KEY]

    def test_each_child_gets_one_handle_line_in_its_own_entry_and_the_result_stays_json(self):
        self.delegate(KETTLE, "c1", "The kettle is at eighty degrees.")
        self.delegate(SHELF, "c2", "The shelf is made of oak.")
        before = self.result({"summary": "The kettle is at eighty degrees."}, {"summary": "The shelf is made of oak."})
        out = self.hook([KETTLE, SHELF], before)
        self.assertIsInstance(out, str)
        data, kept = json.loads(out), json.loads(before)
        roots = [c.meta["root"] for _, c in self.receipts()]
        for entry, root in zip(data["results"], roots):
            self.assertTrue(self.line_of(entry).startswith("%s %s sess" % (LINE, root)), entry)
            self.assertNotIn("\n", self.line_of(entry))  # one line
        for entry in data["results"]:  # nothing else changed: the keys, the values, the order
            del entry[handles.KEY]
        self.assertEqual(data, kept)
        self.assertEqual(list(data), list(kept))
        self.assertEqual(out.count(LINE), 2)  # K children, K lines

    def test_the_model_opens_the_exact_text_of_a_child_from_the_id_in_its_line(self):
        self.delegate(KETTLE, "c1")
        self.delegate(SHELF, "c2")
        data = json.loads(self.hook([KETTLE, SHELF], self.result({}, {})))
        shown = [self.open(self.line_of(e).split()[1], 0.8) for e in data["results"]]  # crumb(id, level), the id of the line
        self.assertIn("kettle", shown[0])
        self.assertNotIn("oak", shown[0])
        self.assertIn("oak", shown[1])
        self.assertNotIn("kettle", shown[1])

    def test_a_line_goes_to_the_task_of_its_goal_whatever_the_order_of_the_calls(self):
        self.delegate(SHELF, "c2")  # on_delegation came in the other order
        self.delegate(KETTLE, "c1")
        data = json.loads(self.hook([KETTLE, SHELF], self.result({}, {})))
        self.assertIn("eighty", self.open(self.line_of(data["results"][0]).split()[1]))
        self.assertIn("oak", self.open(self.line_of(data["results"][1]).split()[1]))

    def test_a_task_that_did_not_run_gets_no_line_and_the_others_keep_theirs(self):
        self.delegate(KETTLE, "c1")
        self.delegate(SHELF, "c2")
        refused = {"status": "error", "summary": None, "error": "no route", "api_calls": 0}
        data = json.loads(self.hook([KETTLE, "a task that was refused", SHELF], self.result({}, refused, {})))
        self.assertEqual([handles.KEY in e for e in data["results"]], [True, False, True])

    def test_tasks_with_one_goal_take_their_lines_in_order_when_the_counts_agree(self):
        self.delegate(KETTLE, "c1")
        self.delegate(KETTLE, "c2")  # the same words for two children
        data = json.loads(self.hook([KETTLE, KETTLE], self.result({}, {})))
        self.assertEqual([self.line_of(e).split()[1] for e in data["results"]],
                         [c.meta["root"] for _, c in self.receipts()])

    def test_tasks_with_one_goal_and_a_missing_offer_get_no_line_rather_than_a_wrong_one(self):
        self.delegate(KETTLE, "c1")  # the second task with this goal was refused: one offer for two tasks
        refused = {"status": "error", "summary": None, "error": "no route", "api_calls": 0}
        self.assertIsNone(self.hook([KETTLE, KETTLE], self.result(refused, {})))

    def test_a_line_is_used_once(self):
        self.delegate(KETTLE, "c1")
        text = self.result({})
        self.assertIsNotNone(self.hook([KETTLE], text))
        self.assertIsNone(self.hook([KETTLE], text))
        self.assertEqual(len(handles._offers), 0)

    def test_another_chat_never_gets_the_line_of_this_one(self):
        self.delegate(KETTLE, "c1")
        self.assertIsNone(self.hook([KETTLE], self.result({}), session="another-chat"))
        self.assertIsNone(self.hook([KETTLE], self.result({}), session=""))  # a chat that the hook cannot name gets none
        self.assertIsNotNone(self.hook([KETTLE], self.result({}), session="p1"))  # the offer waited for its own chat

    def test_a_goal_that_hermes_changed_before_on_delegation_gets_no_line(self):
        """Hermes scrubs secrets from the goal before it calls on_delegation; the arguments of the call keep them."""
        self.delegate("call the API with sk-ant...MnOp", "c1")
        self.assertIsNone(self.hook(["call the API with sk-ant-api03-AbCdEfGhIjKlMnOp"], self.result({})))

    def test_the_result_of_the_joined_fallback_with_its_note_gets_the_lines_and_keeps_its_keys(self):
        self.delegate(KETTLE, "c1")
        text = json.dumps({"results": [{"task_index": 0, "status": "completed", "summary": "s"}],
                           "total_duration_seconds": 0.5, "note": "ran in this call"})
        data = json.loads(self.hook([KETTLE], text))
        self.assertEqual(list(data), ["results", "total_duration_seconds", "note"])
        self.assertIn(handles.KEY, data["results"][0])

    def test_an_offer_that_is_older_than_the_call_is_not_used(self):
        self.delegate(KETTLE, "c1")
        later = handles.time.monotonic() + 100.0
        with mock.patch.object(handles, "time", mock.Mock(monotonic=lambda: later)):
            self.assertIsNone(self.hook([KETTLE], self.result({}), ms=2000))  # the call took 2 s, the offer is 100 s old

    def test_an_offer_that_no_result_took_expires(self):
        self.delegate(KETTLE, "c1")
        later = handles.time.monotonic() + handles.LIFETIME + 1
        with mock.patch.object(handles, "time", mock.Mock(monotonic=lambda: later)):
            handles.offer("p1", "another", LINE + " x")
        self.assertEqual([o[2] for o in handles._offers], ["another"])

    def test_the_hook_changes_no_other_tool_and_no_result_that_has_no_entries(self):
        self.delegate(KETTLE, "c1")
        text = self.result({})
        self.assertIsNone(self.hook([KETTLE], text, tool="terminal"))
        for other in ('{"status": "dispatched", "mode": "background", "count": 1}',  # a call that runs in the background
                      "not json", "[]", '{"results": "none"}', "", None, {"results": []}):
            with self.subTest(result=other):
                self.assertIsNone(self.hook([KETTLE], other))
        self.assertEqual(len(handles._offers), 1)  # none of these used the offer
        self.assertIsNotNone(self.hook([KETTLE], text))

    def test_the_goals_come_from_tasks_as_a_list_as_json_text_or_from_the_one_goal(self):
        self.assertEqual(handles.goals({"tasks": [{"goal": "a"}, {"goal": 5}, "b", {}]}), ["a", None, None, None])
        self.assertEqual(handles.goals({"tasks": json.dumps([{"goal": "a"}, {"goal": "b"}])}), ["a", "b"])
        self.assertEqual(handles.goals({"goal": "a"}), ["a"])
        self.assertEqual(handles.goals({"goal": "a", "tasks": []}), ["a"])  # Hermes reads an empty list as no batch
        self.assertEqual(handles.goals({"goal": "a", "tasks": [{"goal": "b"}]}), ["b"])  # and a batch before the one goal
        for bad in (None, [], "x", {"tasks": "not json"}, {"tasks": {"goal": "a"}}):
            self.assertIsInstance(handles.goals(bad), list)
        entries = json.dumps({"results": [{"task_index": 0, "status": "completed", "summary": "s"}]})
        self.delegate(KETTLE, "c1")
        out = handles.transform_tool_result(tool_name="delegate_task", args={"goal": KETTLE}, result=entries,
                                            session_id="p1", duration_ms=10)
        self.assertIn(handles.KEY, json.loads(out)["results"][0])

    def test_without_the_crumb_tool_the_model_gets_no_line(self):
        self.p.slot = Slot("fake", FakeSlot(), self.p._log)  # a slot, and ACE is not selected: breadcrumbs shows no tools
        self.assertFalse(self.p._shows_own())
        self.delegate(KETTLE, "c1")
        self.assertTrue(self.receipts()[0][1].meta["root"])  # the work is in the store all the same
        self.assertIsNone(self.hook([KETTLE], self.result({})))

    def test_the_hook_never_raises(self):
        self.delegate(KETTLE, "c1")
        text = self.result({})
        with mock.patch.object(handles.json, "dumps", side_effect=ValueError("boom")):
            self.assertIsNone(self.hook([KETTLE], text))
        self.assertIsNone(handles.transform_tool_result())
        self.assertIsNone(handles.transform_tool_result(tool_name="delegate_task", args=7, result=7))

    def test_hermes_calls_the_hook_with_more_keywords_than_it_names(self):
        self.delegate(KETTLE, "c1")
        out = handles.transform_tool_result(
            tool_name="delegate_task", args={"tasks": [{"goal": KETTLE}]}, result=self.result({}), task_id="t1", session_id="p1",
            tool_call_id="tc1", turn_id="u1", api_request_id="r1", conversation_id="p1", duration_ms=900, status="ok",
            error_type=None, error_message=None, telemetry_schema_version=3)
        self.assertIn(handles.KEY, json.loads(out)["results"][0])


class RegisterTest(unittest.TestCase):
    def test_the_hook_is_registered_where_hermes_has_hooks(self):
        got = []

        class Ctx:
            def register_hook(self, name, callback):
                got.append((name, callback))

        register_handles(Ctx())
        self.assertEqual(got, [("transform_tool_result", handles.transform_tool_result)])

    def test_a_hermes_without_hooks_gets_none_and_no_error(self):
        register_handles(object())


if __name__ == "__main__":
    unittest.main()
