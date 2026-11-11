import json
import os
import sqlite3
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.ingest import leaves


class ProviderTest(unittest.TestCase):
    """The provider through its MemoryProvider methods (no Hermes needed)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def call(self, tool, **args):
        return json.loads(self.p.handle_tool_call(tool, args))

    def test_crumb_still_answers_while_another_writer_holds_the_index(self):
        self.p.sync_turn("locked index question", "locked index answer", session_id="s1")
        sess = self.p.session_crumb("s1")
        self.p.index.db.execute("PRAGMA busy_timeout = 100")
        other = sqlite3.connect(os.path.join(self.tmp.name, "breadcrumbs", "index.db"), isolation_level=None)
        other.execute("BEGIN IMMEDIATE")  # another process in the middle of a write
        try:
            out = self.call("mcp__crumb", id=sess, level=1)
        finally:
            other.execute("ROLLBACK")
            other.close()
        self.assertIn("locked index question", out["text"])

    def test_the_prompt_tells_how_to_pick_a_level_from_the_size(self):
        self.assertIn("400 / N", self.p.system_prompt_block())

    def test_the_prompt_names_the_dispatch_handbook_only_when_hermes_registered_it(self):
        self.assertNotIn("dispatch", self.p.system_prompt_block())  # an older Hermes: no plugin skills
        self.p.handbook = "breadcrumbs:dispatch"
        self.assertIn('skill_view("breadcrumbs:dispatch")', self.p.system_prompt_block())

    def test_the_prompt_and_the_tool_say_that_a_level_below_one_shows_key_words(self):
        crumb = [t for t in self.p.get_tool_schemas() if t["name"].endswith("crumb")][0]
        for text in (self.p.system_prompt_block(), crumb["description"]):
            self.assertIn("\u2026", text)
            self.assertIn("words", text)

    def test_name_and_tools(self):
        self.assertEqual(self.p.name, "breadcrumbs")
        self.assertTrue(self.p.is_available())
        names = sorted(t["name"] for t in self.p.get_tool_schemas())
        self.assertEqual([n.removeprefix("mcp__") for n in names], ["crumb", "crumb_find"])

    def test_tool_answers_to_both_wire_and_bare_names(self):
        self.p.sync_turn("wire names round trip", "ok", session_id="s1")
        self.assertEqual(self.p.handle_tool_call("mcp__crumb_find", {"q": "wire names"}),
                         self.p.handle_tool_call("crumb_find", {"q": "wire names"}))

    def test_synced_turn_is_found_and_expands_verbatim(self):
        self.p.sync_turn("pin the graalpy overallocate flag", "done: --python.OverallocateLiteralLists=false",
                         session_id="s1")
        found = self.call("crumb_find", q="graalpy flag")["text"]
        self.assertIn("pin the graalpy overallocate flag", found)
        turn_id = found.split()[1]
        full = self.call("crumb", id=turn_id, level=1.0)["text"]
        self.assertIn("pin the graalpy overallocate flag", full)

    def test_session_crumb_holds_every_turn(self):
        for i in range(3):
            self.p.sync_turn("question %d about zoom levels" % i, "answer %d" % i, session_id="s1")
        sess = self.p.session_crumb("s1")
        text = self.call("crumb", id=sess, level=1.0)["text"]
        for i in range(3):
            self.assertIn("question %d about zoom levels" % i, text)
            self.assertIn("answer %d" % i, text)

    def test_tool_rows_of_the_turn_become_step_and_tool_crumbs(self):
        call = {"id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "notes.txt"}'}}
        msgs = [{"role": "system", "content": "system text"},
                {"role": "user", "content": "an older question"},
                {"role": "assistant", "content": "an older answer"},
                {"role": "user", "content": "what is in notes.txt?"},
                {"role": "assistant", "content": None, "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "kettle at eighty degrees"},
                {"role": "assistant", "content": "It says: kettle at eighty degrees."}]
        self.p.sync_turn("what is in notes.txt?", "It says: kettle at eighty degrees.", session_id="s1", messages=msgs)
        ids = leaves(self.p.store, self.p.session_crumb("s1"))
        crumbs = [get_crumb(self.p.store, c) for c in ids]
        self.assertEqual([c.kind for c in crumbs], ["turn", "step", "tool", "turn"])  # older rows are not stored again
        self.assertEqual(crumbs[2].meta["role"], "tool:read_file")
        self.assertEqual((crumbs[2].meta["call"], crumbs[2].meta["step"]), ("call_1", ids[1]))
        self.assertIn('→ read_file path: notes.txt', self.call("crumb", id=ids[1], level=1.0)["text"])
        self.assertIn(ids[2], self.call("crumb_find", q="eighty degrees kettle")["text"])

    def test_memory_tool_rows_are_stored_but_never_searched(self):
        """The calls and results of crumb and crumb_find copy memory. Indexed again, they rank above their
        own source when a question comes back (an echo). They stay in the session record."""
        mem = {"id": "call_1", "type": "function",
               "function": {"name": "mcp__crumb_find", "arguments": '{"q": "walrus kettle"}'}}
        other = {"id": "call_2", "type": "function",
                 "function": {"name": "terminal", "arguments": '{"command": "cat walrus.txt"}'}}
        msgs = [{"role": "user", "content": "what about the walrus kettle?"},
                {"role": "assistant", "content": None, "tool_calls": [mem]},
                {"role": "tool", "tool_call_id": "call_1", "name": "mcp__crumb_find", "content": "walrus kettle hits"},
                {"role": "assistant", "content": None, "tool_calls": [other]},
                {"role": "tool", "tool_call_id": "call_2", "name": "terminal", "content": "walrus kettle file text"},
                {"role": "assistant", "content": "The walrus kettle is in the file."}]
        self.p.sync_turn("what about the walrus kettle?", "The walrus kettle is in the file.", session_id="s1",
                         messages=msgs)
        ids = leaves(self.p.store, self.p.session_crumb("s1"))
        self.assertEqual([get_crumb(self.p.store, c).kind for c in ids], ["turn", "step", "tool", "step", "tool", "turn"])
        hits = {crumb for crumb, _, _ in self.p.index.search("walrus kettle", 20)}
        self.assertEqual(hits & {ids[1], ids[2]}, set())  # the memory call and its result
        self.assertLessEqual({ids[3], ids[4]}, hits)  # the terminal call and its result

    def test_an_answer_after_memory_calls_only_links_to_its_question(self):
        """The neighbor chain skips rows that are not indexed: the answer's neighbor is its question."""
        mem = {"id": "call_1", "type": "function", "function": {"name": "mcp__crumb", "arguments": '{"id": "x"}'}}
        msgs = [{"role": "user", "content": "the walrus kettle again?"},
                {"role": "assistant", "content": None, "tool_calls": [mem]},
                {"role": "tool", "tool_call_id": "call_1", "name": "mcp__crumb", "content": "walrus kettle text"},
                {"role": "assistant", "content": "Yes, the walrus kettle."}]
        self.p.sync_turn("the walrus kettle again?", "Yes, the walrus kettle.", session_id="s1", messages=msgs)
        user, step, tool, answer = leaves(self.p.store, self.p.session_crumb("s1"))
        nbr = lambda c: {b for (b,) in self.p.index.db.execute("SELECT b FROM nbr WHERE a = ?", (c,))}
        self.assertEqual((nbr(answer), nbr(step), nbr(tool)), ({user}, set(), set()))

    def test_cue_words_skip_the_calls_and_results_of_memory_tools(self):
        """Cue words name a session's topic. A memory search copies other sessions: its tool name and its
        hits would link this session to every session that searched (\"crumb\" was a hub word)."""
        mem = {"id": "call_1", "type": "function",
               "function": {"name": "mcp__crumb_find", "arguments": '{"q": "walrus kettle"}'}}
        msgs = [{"role": "user", "content": "where is the walrus kettle?"},
                {"role": "assistant", "content": None, "tool_calls": [mem]},
                {"role": "tool", "tool_call_id": "call_1", "name": "mcp__crumb_find", "content": "zebra quokka hits"},
                {"role": "assistant", "content": "The walrus kettle is on the shelf."}]
        self.p.sync_turn("plain bread question", "plain bread answer", session_id="s1")  # other text first:
        self.p.on_session_switch("s2")  # a text alone in the index has no cue words, and the zoom keeps them
        self.p.sync_turn("where is the walrus kettle?", "The walrus kettle is on the shelf.", session_id="s2",
                         messages=msgs)
        cues = self.p.zoom.cue_words(self.p.session_crumb("s2")).lower().split()
        self.assertIn("walrus", cues)
        self.assertFalse({"mcp", "crumb", "find", "zebra", "quokka", "hits"} & set(cues), cues)

    def test_without_messages_a_turn_is_two_crumbs(self):
        self.p.sync_turn("plain question", "plain answer", session_id="s1")
        kinds = [get_crumb(self.p.store, c).kind for c in leaves(self.p.store, self.p.session_crumb("s1"))]
        self.assertEqual(kinds, ["turn", "turn"])

    def test_below_level_one_a_crumb_shows_words_and_no_grammar(self):
        self.p.sync_turn("the kettle boils the water too fast for the green tea leaves",
                         "steep the tea for two minutes and then the leaves are done", session_id="s1")
        for i in range(4):
            self.p.sync_turn("the bike chain skips on the small cog %d" % i, "the chain is worn", session_id="o%d" % i)
        turn = get_crumb(self.p.store, self.p.session_crumb("s1")).kids[0]
        out = self.call("crumb", id=turn, level=0.9)["text"]
        words = [w for l in out.splitlines()[1:] for w in l.lower().split()]  # the lines under the header
        self.assertIn("kettle", words)
        self.assertNotIn("the", words)
        self.assertIn("the kettle boils the water", self.call("crumb", id=turn, level=1.0)["text"])

    def test_prefetch_recalls_other_sessions_small(self):
        self.p.sync_turn("the build host runs the load tests", "noted", session_id="old")
        self.p.on_session_switch("s2", reset=True)
        out = self.p.prefetch("where do the load tests run?", session_id="s2")
        self.assertIn("build host", out)
        self.assertLessEqual(len(out), 4 * self.p.budget + 200)

    def test_find_returns_the_reply_next_to_a_matching_question(self):
        self.p.sync_turn("where do the nightly load tests run?", "On the build host, after midnight.", session_id="s1")
        self.assertIn("On the build host, after midnight.", self.call("crumb_find", q="nightly load tests")["text"])

    def test_find_shows_each_hit_under_its_session_handle_with_cue_words(self):
        self.p.sync_turn("the kettle boils; steep the tea four minutes", "noted: kettle and tea", session_id="old")
        self.p.sync_turn("the parser crashed on an empty line", "fixed the parser", session_id="s1")
        lines = self.call("crumb_find", q="kettle tea")["text"].splitlines()
        heads = [l for l in lines if l.startswith("\u25b8 ")]
        self.assertEqual([h.split()[1] for h in heads], [self.p.session_crumb("old")])
        self.assertIn("kettle", heads[0].partition(" | ")[2].split())
        self.assertEqual(sum(1 for l in lines if l.startswith("  \u25b8 ")), 2)

    def test_prefetch_skips_trivial_prompts(self):
        self.p.sync_turn("anything", "else", session_id="s1")
        self.assertEqual(self.p.prefetch("ok", session_id="s1"), "")

    def test_memory_write_is_mirrored_as_a_crumb(self):
        self.p.on_memory_write("add", "user", "The user wants Simplified Technical English")
        self.assertIn("Simplified Technical English", self.call("crumb_find", q="technical english")["text"])

    def test_pre_compress_returns_expandable_handles(self):
        self.p.sync_turn("long design talk about merkle spines", "agreed", session_id="s1")
        out = self.p.on_pre_compress([])
        self.assertIn(self.p.session_crumb("s1"), out)
        self.assertLessEqual(len(out), 6000)

    def test_unknown_id_is_an_error_not_a_crash(self):
        self.assertIn("error", self.call("crumb", id="deadbeef", level=0.5))

    def test_non_primary_context_writes_nothing(self):
        q = BreadcrumbsProvider()
        q.initialize("s9", hermes_home=self.tmp.name + "/sub", platform="cli", agent_context="subagent")
        q.sync_turn("secret subagent chatter", "x", session_id="s9")
        self.assertNotIn("subagent chatter", json.loads(q.handle_tool_call("crumb_find", {"q": "subagent chatter"}))["text"])
        q.shutdown()

    def test_store_survives_restart(self):
        self.p.sync_turn("remember the crc32c ordinal rule", "ok", session_id="s1")
        self.p.shutdown()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli")
        self.assertIn("crc32c ordinal rule", self.call("crumb_find", q="ordinal rule")["text"])
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "breadcrumbs", "store")))


BIG = [{"role": "user", "content": "x" * 4000}]  # a context much larger than any recall


class HorizonProviderTest(unittest.TestCase):
    """The steady-state recall budget, driven by the provider's own hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def notes(self, n, session="old"):
        for i in range(n):
            self.p.sync_turn("load test note %d: the build host runs the load tests every night" % i,
                             "noted", session_id=session, messages=BIG)

    def test_compactions_shrink_the_recall_budget(self):
        start = self.p.ctl.budget
        for i in range(10):
            self.p.on_pre_compress([])
            self.p.sync_turn("question %d" % i, "answer %d" % i, session_id="s1", messages=BIG)
        self.assertLess(self.p.ctl.budget, start)

    def test_clean_turns_grow_the_recall_budget(self):
        start = self.p.ctl.budget
        self.notes(10, session="s1")
        self.assertGreater(self.p.ctl.budget, start)

    def test_recall_that_crowds_the_context_is_an_overflow(self):
        self.notes(30)
        self.p.on_session_switch("s2", reset=True)
        start = self.p.ctl.budget
        for i in range(4):  # one crowded turn sits inside the 2%..5% band; repeated ones cut
            self.assertTrue(self.p.prefetch("where do the load tests run", session_id="s2"))
            self.p.sync_turn("where do the load tests run %d" % i, "on the build host", session_id="s2",
                             messages=[{"role": "user", "content": "where do the load tests run"}])
        self.assertLess(self.p.ctl.budget, start)

    def test_prefetch_size_follows_the_budget(self):
        self.notes(40)
        self.p.on_session_switch("s2", reset=True)
        self.p.ctl.budget = 400
        big = self.p.prefetch("build host load tests", session_id="s2")
        self.p.ctl.budget = 60
        small = self.p.prefetch("build host load tests", session_id="s2")
        self.assertLess(len(small), len(big))
        self.assertLessEqual(len(small.encode()), 4 * 60 + 400)

    def test_controller_state_survives_restart(self):
        self.notes(5, session="s1")
        budget = self.p.ctl.budget
        self.p.shutdown()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli")
        self.assertEqual(self.p.ctl.budget, budget)

    def test_an_expanded_crumb_ranks_first_later(self):
        t0 = 1_000_000_000
        self.p.clock = lambda: t0
        self.p.sync_turn("alpha bravo charlie one", "", session_id="old")
        self.p.sync_turn("alpha bravo charlie two", "", session_id="old")
        rows = json.loads(self.p.handle_tool_call("mcp__crumb_find", {"q": "alpha bravo charlie"}))["text"].splitlines()
        two = next(r.split()[1] for r in rows if r.lstrip().startswith("\u25b8") and "two" in r)
        self.p.clock = lambda: t0 + 30 * 86400  # a month later the model expands one of them
        self.p.handle_tool_call("mcp__crumb", {"id": two, "level": 1})
        self.p.on_session_switch("s2", reset=True)
        out = self.p.prefetch("alpha bravo charlie", session_id="s2")
        self.assertLess(out.index("charlie two"), out.index("charlie one"))


if __name__ == "__main__":
    unittest.main()
