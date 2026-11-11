"""ACE (adaptive context engineering): older turns of a chat become a finite layer of crumbs, each at a
level from 0 (handle) to 1 (exact text); a crumb the model opens grows and its neighbors shrink."""
import json
import math
import os
import re
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import ace


def entry(eid, group, size, weight, floor=0.0):
    return {"id": eid, "group": group, "size": size, "weight": weight, "floor": floor}


def chat(turns, tool_lines=90, start=0):
    """A neutral chat: each turn asks, runs one tool with a long result, and answers."""
    out = []
    for t in range(start, start + turns):
        out.append({"role": "user", "content": "turn %d: where is the walrus kettle number %d?" % (t, t)})
        out.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c%d" % t, "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "ls shelf-%d" % t})}}]})
        out.append({"role": "tool", "tool_call_id": "c%d" % t,
                    "content": "".join("shelf %d row %d holds a green kettle\n" % (t, i) for i in range(tool_lines))})
        out.append({"role": "assistant", "content": "Kettle %d is on shelf %d, row 7." % (t, t)})
    return out


def long_turn(steps, lines=60, think=0, start=0, ask=True):
    """One user turn in which the agent works: each step calls a tool and reads its long result."""
    out = [{"role": "user", "content": "read every shelf and count the copper kettles"}] if ask else []
    for s in range(start, start + steps):
        call = {"role": "assistant", "content": "", "tool_calls": [{"id": "s%d" % s, "type": "function", "function": {
            "name": "terminal", "arguments": json.dumps({"command": "cat shelf-%d" % s})}}]}
        if think:
            call["reasoning_details"] = [{"type": "thinking", "thinking": "walrus " * think, "signature": "x" * 4000}]
        out.append(call)
        out.append({"role": "tool", "tool_call_id": "s%d" % s,
                    "content": "".join("shelf %d row %d holds a copper kettle\n" % (s, i) for i in range(lines))})
    return out


def pairs(messages):
    """The tool call ids of the request, and the ids that its tool results answer."""
    return ({c["id"] for m in messages for c in m.get("tool_calls") or []},
            {m["tool_call_id"] for m in messages if m.get("role") == "tool"})


SYSTEM = {"role": "system", "content": "You are a test agent."}


class SizeTest(unittest.TestCase):
    def test_the_size_counts_the_thinking_that_goes_back_once_and_no_signature(self):
        m = {"role": "assistant", "content": "ok", "reasoning": "a" * 5000, "reasoning_content": "a" * 5000,
             "reasoning_details": [{"type": "thinking", "thinking": "a" * 5000, "signature": "s" * 20000}]}
        self.assertEqual(ace.request_bytes([m]), 2 + 5000 + 16)
        stored = dict(m, reasoning_details=json.dumps(m["reasoning_details"]))  # state.db keeps it as JSON text
        self.assertEqual(ace.request_bytes([stored]), 2 + 5000 + 16)
        self.assertEqual(ace.request_bytes([{"role": "assistant", "content": "ok", "reasoning": "b" * 300}]), 318)


class AllocateTest(unittest.TestCase):
    def test_the_layer_stays_inside_its_budget_and_levels_are_steps(self):
        es = [entry("a%d" % i, i // 4, 2000, 1.0 / (1 + i)) for i in range(12)]
        lv = ace.allocate(es, 6000)
        self.assertLessEqual(ace.layer_cost(es, lv), 6000)
        self.assertTrue({v for v in lv.values() if v is not None} <= set(ace.LEVELS))

    def test_dense_important_text_gets_the_room(self):
        lv = ace.allocate([entry("small-hot", 0, 400, 5.0), entry("big-cold", 0, 40000, 0.2)], 2000)
        self.assertEqual(lv["small-hot"], 1.0)
        self.assertLess(lv["big-cold"], 0.1)

    def test_growing_one_crumb_shrinks_its_own_group_first(self):
        es = [entry("g0-%d" % i, 0, 3000, 1.0) for i in range(4)] + [entry("g1-%d" % i, 1, 3000, 1.0) for i in range(4)]
        before = ace.allocate(es, 8000)
        es[0]["floor"] = 1.0
        after = ace.allocate(es, 8000)
        self.assertEqual(after["g0-0"], 1.0)
        def lost(g):
            return sum(ace.cost(e, before[e["id"]]) - ace.cost(e, after[e["id"]]) for e in es[1:] if e["group"] == g)
        self.assertGreater(lost(0), lost(1))

    def test_when_the_handles_do_not_fit_the_oldest_groups_fold_into_one_line(self):
        es = [entry("e%d" % i, i // 10, 500, 1.0) for i in range(100)]
        lv = ace.allocate(es, 1400)
        self.assertLessEqual(ace.layer_cost(es, lv), 1400)
        hidden = {e["group"] for e in es if lv[e["id"]] is None}
        shown = {e["group"] for e in es if lv[e["id"]] is not None}
        self.assertTrue(hidden and shown and max(hidden) < min(shown))

    def test_the_group_of_an_opened_crumb_folds_last(self):
        es = [entry("e%d" % i, i // 10, 500, 1.0) for i in range(100)]
        es[3]["floor"] = 0.5  # in group 0, the oldest
        lv = ace.allocate(es, 1400)
        self.assertLessEqual(ace.layer_cost(es, lv), 1400)
        self.assertIsNotNone(lv["e3"])
        self.assertGreater(lv["e3"] or 0, 0)


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_small_target_keeps_more_exact_text_and_a_large_one_more_crumbs(self):
        # bench/ace_paired.py, LongMemEval-S (DESIGN 9.15): up to SMALL_TARGET tokens the small layer recalls more
        req = [SYSTEM] + chat(200)
        for tokens, small in ((ace.SMALL_TARGET, True), (2 * ace.SMALL_TARGET, False)):
            e = ace.Ace(target_tokens=tokens)
            e.on_session_start("s1", hermes_home=tempfile.mkdtemp(dir=self.tmp.name))
            self.addCleanup(e.close)
            out = e.select_context(req, budget_tokens=1_000_000)
            layer = len(out[1]["content"].encode("utf-8"))  # the layer, then the oldest kept user message
            tail = ace.request_bytes(out[2:])
            self.assertEqual(tail > 3 * layer, small, (tokens, tail, layer))


class LayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = ace.Ace(target_tokens=16000)  # under SMALL_TARGET: the small layer share
        self.engine.on_session_start("s1", hermes_home=self.tmp.name)
        self.addCleanup(self.engine.close)

    def select(self, messages):
        return self.engine.select_context(messages, budget_tokens=1_000_000)

    def test_under_the_target_the_request_is_not_changed(self):
        self.assertIsNone(self.select([SYSTEM] + chat(3)))

    def test_over_the_target_older_turns_become_a_layer_and_the_tail_stays_exact(self):
        req = [SYSTEM] + chat(40)
        out = self.select(req)
        self.assertEqual(out[0], SYSTEM)
        first = out[1]
        self.assertEqual(first["role"], "user")
        self.assertTrue(first["content"].startswith(ace.LAYER_HEAD))
        tail = out[2:]
        self.assertEqual(tail, req[len(req) - len(tail):])
        user = max(i for i in range(len(req) - len(tail)) if req[i]["role"] == "user")  # the oldest kept turn
        self.assertTrue(first["content"].endswith(req[user]["content"]))
        self.assertLessEqual(self.engine.tokens(ace.request_bytes(out)), ace.FOLD_TO * self.engine.target_tokens)

    def test_every_crumb_in_the_layer_opens_to_the_exact_text(self):
        req = [SYSTEM] + chat(240)  # so long that the oldest groups fold into one crumb each, at any fold share
        layer = self.select(req)[1]["content"]
        texts = {ace.crumb_text(m) for m in req}
        refs = ace.REF_RE.findall(layer)
        groups = 0
        for ref in refs:
            exact = self.engine.zoom.render(ref, 1.0)
            if exact in texts:
                continue
            groups += 1
            # an old group that folded into one crumb: its messages in order, each exact under its own ref and role
            line = next(x for x in layer.split("\n") if ref in x)
            a, b = map(int, re.search(r"\| turns (\d+)-(\d+) \|", line).groups())
            members = req[1 + 4 * (a - 1):1 + 4 * b]  # chat(): four messages a turn
            self.assertEqual(exact, "".join("%s %s %s\n%s\n" % (ace.GAP, ace.ref_of(self.engine.store, ace.crumb_text(m)), role,
                                                                ace.crumb_text(m))
                                            for m, role in zip(members, ["user", "call", "tool", "assistant"] * (b - a + 1))))
        self.assertGreater(groups, 0)
        self.assertGreater(len(refs) - groups, 20)

    def test_the_layer_does_not_change_until_the_next_fold(self):
        req = [SYSTEM] + chat(40)
        first = self.select(req)[1]["content"]
        again = self.select(req + [{"role": "user", "content": "and the next one?"}])
        self.assertEqual(again[1]["content"], first)

    def test_a_crumb_the_model_opens_grows_in_the_next_layer(self):
        req = [SYSTEM] + chat(40)
        layer = self.select(req)[1]["content"]
        tool_ref = next(r for r in ace.REF_RE.findall(layer) if self.engine.levels.get(r, 1) < 0.5)
        opened = [{"role": "user", "content": "check that shelf again"},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "type": "function", "function": {
                      "name": "mcp__crumb", "arguments": json.dumps({"id": tool_ref, "level": 0.5})}}]},
                  {"role": "tool", "tool_call_id": "x1", "content": self.engine.zoom.render(tool_ref, 0.5)},
                  {"role": "assistant", "content": "Row 7 holds it."}]
        self.select(req + opened + chat(30, start=40))  # enough new turns to fold again
        self.assertGreaterEqual(self.engine.levels[tool_ref], 0.5)

    def test_the_same_text_twice_is_one_crumb(self):
        req = [SYSTEM] + chat(40)
        req[83]["content"] = req[87]["content"]  # turn 20 and turn 21 got the same tool result
        layer = self.select(req)[1]["content"]
        ref = ace.ref_of(self.engine.store, ace.crumb_text(req[87]))
        self.assertEqual(layer.count(ref), 1)
        self.assertIn("\u00d72", layer)

    def test_the_model_counts_set_the_fixed_part_then_the_tokens_per_byte(self):
        req = [SYSTEM] + chat(6)
        self.assertIsNone(self.select(req))  # under the target by the first estimate
        sent = ace.request_bytes(req)
        first = int(1.2 * self.engine.target_tokens)  # the tool schemas are in the count, not in the bytes
        self.engine.update_from_response({"prompt_tokens": first})
        self.assertEqual(self.engine.base, int(first - ace.TPB * sent))
        self.assertAlmostEqual(self.engine.tokens(sent), first, delta=1)  # both readings give the count back
        self.assertAlmostEqual(self.engine.ratio * sent, first, delta=1)
        out = self.select(req)  # the same request is now over the target
        self.assertTrue(out[1]["content"].startswith(ace.LAYER_HEAD))
        self.engine.update_from_response({"prompt_tokens": (self.engine.base or 0) + ace.request_bytes(out) // 2})
        self.assertAlmostEqual(self.engine.tpb, 0.5, places=2)  # the next count: the tokens per byte of the rest
        n = 10 * ace.request_bytes(out)
        self.assertEqual(self.engine.tokens(n), max(self.engine.ratio * n, (self.engine.base or 0) + self.engine.tpb * n))

    def test_one_long_turn_folds_its_first_steps_and_keeps_its_question_and_newest_steps(self):
        req = [SYSTEM] + chat(2) + long_turn(60, think=200)
        out = self.select(req)
        first = out[1]
        self.assertEqual((out[0], first["role"]), (SYSTEM, "user"))
        self.assertTrue(first["content"].startswith(ace.LAYER_HEAD))
        self.assertTrue(first["content"].endswith("read every shelf and count the copper kettles"))
        self.assertEqual(out[2]["role"], "assistant")  # the kept steps start at a call
        tail = out[2:]
        self.assertEqual(tail, req[len(req) - len(tail):])  # the newest steps, exact and in order
        self.assertLess(len(tail), 120)
        calls, results = pairs(out)
        self.assertEqual(calls, results)  # no call without its result, no result without its call
        self.assertLessEqual(self.engine.tokens(ace.request_bytes(out)), self.engine.target_tokens)
        texts = {ace.crumb_text(m) for m in req}
        refs = ace.REF_RE.findall(first["content"])
        self.assertTrue(refs)
        for ref in refs:  # each folded step opens to its exact text
            self.assertIn(self.engine.zoom.render(ref, 1.0), texts)

    def test_after_a_fold_the_layer_holds_while_the_request_grows(self):
        req = [SYSTEM] + chat(2) + long_turn(60)
        first = self.select(req)[1]["content"]
        folds = self.engine.folds
        again = self.select(req + long_turn(3, start=60, ask=False))
        self.assertEqual((again[1]["content"], self.engine.folds), (first, folds))  # the prompt cache holds
        self.assertEqual(again[-6:], long_turn(3, start=60, ask=False))

    def test_each_request_writes_one_line_to_the_ace_log(self):
        self.select([SYSTEM] + chat(40))
        self.select([SYSTEM] + chat(3))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn(" act=fold ", lines[0])
        self.assertIn(" act=pass ", lines[1])
        for key in (" sess=s1 ", " in=", " out=", " last=", " base=", " tpb="):
            self.assertIn(key, lines[0])

    def test_words_of_the_new_message_raise_an_old_crumb(self):
        req = [SYSTEM] + chat(16)  # just over the target: the first fold, with room above the handles
        req[7]["content"] = "".join("shelf 1 row %d holds the porcelain teapot\n" % i for i in range(90))
        req[11]["content"] = "".join("shelf 2 row %d holds the tin funnel\n" % i for i in range(90))  # turns 1, 2
        req.append({"role": "user", "content": "where did the porcelain teapot go?"})
        self.assertIsNotNone(self.select(req))
        lv = self.engine.levels
        hot, cold = (ace.ref_of(self.engine.store, req[i]["content"]) for i in (7, 11))
        self.assertGreater(lv[hot], lv[cold])
        self.assertIn("%s %s tool" % (ace.HANDLE, hot), self.engine.layer)
        self.assertIn("\u25c6", self.engine.layer.split(hot, 1)[1].split("\n", 1)[0])  # marked as relevant



class LargeShareLayerTest(LayerTest):
    """The layer tests again with the shares of a target over SMALL_TARGET (Hermes's 200k)."""
    def setUp(self):
        small, ace.SMALL_TARGET = ace.SMALL_TARGET, 0
        self.addCleanup(setattr, ace, "SMALL_TARGET", small)
        super().setUp()

class EngineTest(unittest.TestCase):
    def test_it_keeps_the_token_state_and_reports_the_layer(self):
        e = ace.Ace()
        self.assertEqual(e.name, "breadcrumbs")
        e.update_from_response({"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105})
        self.assertEqual((e.last_prompt_tokens, e.last_completion_tokens), (100, 5))
        e.threshold_tokens, e.context_length = 1000, 4000
        self.assertFalse(e.should_compress(500))
        self.assertTrue(e.should_compress(1500))
        for key in ("last_prompt_tokens", "threshold_tokens", "context_length", "usage_percent", "compression_count",
                    "ace_target_tokens", "ace_folds", "ace_layer_crumbs", "ace_layer_bytes"):
            self.assertIn(key, e.get_status())

    def test_a_clone_shares_no_state(self):
        e = ace.Ace(target_tokens=5000)
        c = e.clone_for_agent()
        self.assertIsNot(c.levels, e.levels)
        self.assertEqual(c.target_tokens, 5000)

    def test_the_store_config_sets_the_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "breadcrumbs"))
        with open(os.path.join(tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"ace_tokens": 12345}, f)
        e = ace.Ace()
        e.on_session_start("s1", hermes_home=tmp.name)
        self.addCleanup(e.close)
        e.select_context([SYSTEM] + chat(1))
        self.assertEqual(e.target_tokens, 12345)
        self.assertEqual(ace.Ace(target_tokens=777).target_tokens, 777)

    def test_it_takes_the_name_of_the_plugin_that_holds_it(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "config.yaml"), "w") as f:
            f.write("context:\n  engine: bcdev2\n")
        self.assertTrue(ace.wanted(tmp.name, name="bcdev2"))
        self.assertFalse(ace.wanted(tmp.name))
        e = ace.Ace(name="bcdev2")
        self.assertEqual((e.name, e.clone_for_agent().name), ("bcdev2", "bcdev2"))

    def test_a_setting_after_a_column_0_comment_in_its_block_is_found(self):
        # Hermes's config template puts banner comments at column 0 inside a block (memory: ... # ====
        # ... provider: breadcrumbs). In YAML a comment does not end a block.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "config.yaml"), "w") as f:
            f.write("memory:\n  nudge_interval: 10\n\n# =====\n# Session Continuity\n#\n  provider: breadcrumbs\n"
                    "context:\n# a note\n  engine: breadcrumbs\nother:\n  provider: x\n")
        self.assertEqual(ace.setting(tmp.name, "memory", "provider"), "breadcrumbs")
        self.assertEqual(ace.setting(tmp.name, "context", "engine"), "breadcrumbs")
        self.assertIsNone(ace.setting(tmp.name, "context", "provider"))  # the next block ends the block

    def test_compress_folds_without_a_model(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e = ace.Ace(target_tokens=8000)
        e.on_session_start("s1", hermes_home=tmp.name)
        self.addCleanup(e.close)
        req = [SYSTEM] + chat(40)
        out = e.compress(req, current_tokens=ace.request_bytes(req) // 4, force=True)
        self.assertLess(len(out), len(req))
        self.assertTrue(out[1]["content"].startswith(ace.LAYER_HEAD))
        self.assertEqual(e.compression_count, 1)


class PluginTest(unittest.TestCase):
    """Hermes runs one context-engine plugin per process: register ACE only where the profile selects it."""

    def load(self, config):
        import importlib.util
        import sys
        from unittest import mock
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "config.yaml"), "w") as f:
            f.write(config)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec = importlib.util.spec_from_file_location("bc_plugin_ace", os.path.join(root, "__init__.py"),
                                                      submodule_search_locations=[root])
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["bc_plugin_ace"] = mod
        self.addCleanup(sys.modules.pop, "bc_plugin_ace", None)
        spec.loader.exec_module(mod)
        got = {}

        class Ctx:
            def register_memory_provider(self, p):
                got["memory"] = p.name

            def register_context_engine(self, e):
                got["engine"] = e.name

        with mock.patch.dict(os.environ, {"HERMES_HOME": tmp.name}):
            mod.register(Ctx())
        return got

    def test_a_profile_that_selects_it_gets_the_engine_next_to_the_provider(self):
        got = self.load("model: x\ncontext:\n  engine: breadcrumbs\n  other: 1\nmemory:\n  provider: breadcrumbs\n")
        self.assertEqual(got, {"memory": "breadcrumbs", "engine": "breadcrumbs"})

    def test_another_engine_keeps_its_seat(self):
        got = self.load("context:\n  engine: lcm\n")
        self.assertEqual(got, {"memory": "breadcrumbs"})


FIXED = 62_899  # tokens of the fixed part of a request (the system prompt and the tool list): the bytes do not hold it


def model_count(nbytes, rate=ace.TPB):
    """The model's own count of a request of nbytes: the fixed part, and the tokens of the bytes."""
    return FIXED + math.ceil(rate * nbytes)


def log_line(sess, base=None, tpb="0.3038", r="0.4777"):
    """One whole line of ace.log, in the form that Ace._log writes. With no base given: the counts of GOOD."""
    return ("2026-01-02T03:04:05 pid=7 sess=%s act=keep in=300000 out=110000 last=170000 base=%s tpb=%s r=%s "
            "folds=1 layer=40\n" % (sess, FIXED if base is None else base, tpb, r))


class FirstFoldTest(unittest.TestCase):
    """A new engine object has no model count of its own. It reads the counts that the object before it wrote
    to ace.log for the same session, so its first fold counts the fixed part of the request."""
    SESSION = "local-0123456789abcdef01"  # 24 characters: a log line holds a session id up to this length
    GOOD = (FIXED, 0.3038, 0.4777)        # the counts in log_line()
    NEW = (None, ace.TPB, ace.TPB)        # an object with no count
    OTHER = (11111, "0.2222", "0.3333")   # counts that no test expects back

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def engine(self, session=SESSION, target=200_000):
        e = ace.Ace(target_tokens=target)
        e.on_session_start(session, hermes_home=self.tmp.name)
        self.addCleanup(e.close)
        return e

    def write_log(self, text, name="ace.log"):
        root = os.path.join(self.tmp.name, "breadcrumbs")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, name), "wb") as f:
            f.write(text if isinstance(text, bytes) else text.encode("utf-8"))

    def counts(self, session=SESSION, target=200_000, context=0):
        """The counts of a new object after its first request. context: the context length that Hermes gives it."""
        e = self.engine(session, target)
        if context:
            e.update_model("a-model", context)
        e.select_context([SYSTEM] + chat(1), budget_tokens=1_000_000)
        return e.base, e.tpb, e.ratio

    def test_the_first_fold_of_a_new_object_counts_the_fixed_part(self):
        req = [SYSTEM] + chat(64, tool_lines=400)  # about 1 MB: over the 200,000 target with and without the fixed part
        one = self.engine()
        sent = ace.request_bytes(one.select_context(req, budget_tokens=1_000_000))  # no count yet: this fold is too large
        one.update_from_response({"prompt_tokens": model_count(sent)})
        self.assertEqual(one.base, FIXED)
        one.on_session_reset()  # the same messages again: object 1 now folds with the model's count, and logs it
        first = one.select_context(req, budget_tokens=1_000_000)
        one.close()
        two = self.engine()  # the same root and session, and no model count of its own
        second = two.select_context(req, budget_tokens=1_000_000)
        a, b = (one.tokens(ace.request_bytes(out)) for out in (first, second))  # both by object 1's count
        self.assertAlmostEqual(b, a, delta=0.05 * a)
        self.assertLessEqual(model_count(ace.request_bytes(second)), ace.FOLD_TO * 200_000)

    def test_a_new_object_reads_the_counts_that_the_object_before_it_logged(self):
        req = [SYSTEM] + chat(3)
        size = ace.request_bytes(req)
        one = self.engine()
        one.select_context(req, budget_tokens=1_000_000)
        one.update_from_response({"prompt_tokens": model_count(size)})        # the first count: the fixed part
        one.select_context(req, budget_tokens=1_000_000)
        one.update_from_response({"prompt_tokens": model_count(size, 0.45)})  # the next count: the tokens per byte
        one.select_context(req, budget_tokens=1_000_000)                       # this line holds all three
        one.close()
        self.assertEqual(one.base, FIXED)
        self.assertAlmostEqual(one.tpb, 0.45, places=3)
        base, tpb, ratio = self.counts()
        self.assertEqual(base, FIXED)
        self.assertAlmostEqual(tpb, one.tpb, places=4)
        self.assertAlmostEqual(ratio, one.ratio, places=4)

    def test_the_first_count_of_the_new_object_keeps_the_fixed_part_and_sets_its_tokens_per_byte(self):
        self.write_log(log_line(self.SESSION))
        req = [SYSTEM] + chat(3)
        e = self.engine()
        e.select_context(req, budget_tokens=1_000_000)
        e.update_from_response({"prompt_tokens": model_count(ace.request_bytes(req), 0.45)})
        self.assertEqual(e.base, FIXED)
        self.assertAlmostEqual(e.tpb, 0.45, places=3)

    def test_an_object_that_has_a_count_of_its_own_keeps_it(self):
        req = [SYSTEM] + chat(3)
        e = self.engine()
        e.select_context(req, budget_tokens=1_000_000)
        e.update_from_response({"prompt_tokens": model_count(ace.request_bytes(req))})
        self.write_log(log_line(self.SESSION, *self.OTHER))
        e.on_session_start(self.SESSION, hermes_home=self.tmp.name)  # the store closes; the next request opens it again
        e.select_context(req, budget_tokens=1_000_000)
        self.assertEqual((e.base, e.tpb), (FIXED, ace.TPB))

    def test_compress_on_a_new_object_reads_the_counts_too(self):
        self.write_log(log_line(self.SESSION))
        e = self.engine()
        e.compress([SYSTEM] + chat(3))
        self.assertEqual((e.base, e.tpb, e.ratio), self.GOOD)

    def test_a_session_with_no_line_in_the_log_starts_with_no_count_as_before(self):
        self.write_log("".join(log_line(s) for s in ("local-fedcba9876543210ff", "s12", "xs1", "s", "s1 x", "-")))
        self.assertEqual(self.counts(session="s1"), self.NEW)
        self.assertEqual(self.counts(session="s.2"), self.NEW)  # the id is text, not a pattern: "s12" is another session

    def test_an_object_with_no_session_id_reads_nothing_back(self):
        self.write_log(log_line("-"))
        self.assertEqual(self.counts(session=""), self.NEW)

    def test_a_long_session_id_matches_by_the_24_characters_that_the_log_holds(self):
        self.write_log(log_line(self.SESSION))
        self.assertEqual(self.counts(session=self.SESSION + "-and-more"), self.GOOD)

    def test_a_log_with_windows_line_ends_is_read(self):
        self.write_log((log_line(self.SESSION, *self.OTHER) + log_line(self.SESSION)).replace("\n", "\r\n"))
        self.assertEqual(self.counts(), self.GOOD)

    def test_a_damaged_or_half_written_line_is_ignored(self):
        s = self.SESSION
        other = log_line(s, *self.OTHER)
        for name, bad in (("fields are missing", "sess=%s base=11111 tpb=0.2222 r=0.3333\n" % s),
                          ("a field is not a number", log_line(s, "1x111", *self.OTHER[1:])),
                          ("a byte that is no text", other.encode("utf-8").replace(b"11111", b"11\xff11")),
                          ("text before the line", "x" + other),
                          ("text after the line", other[:-1] + " x\n"),
                          ("cut inside a number, with no line end", other[:other.index("r=0.3333") + 5]),
                          ("whole, but with no line end", other[:-1])):
            with self.subTest(name):
                self.write_log(log_line(s).encode("utf-8") + (bad if isinstance(bad, bytes) else bad.encode("utf-8")))
                self.assertEqual(self.counts(), self.GOOD)  # the whole line before it counts

    def test_counts_outside_their_bounds_are_ignored(self):
        s = self.SESSION
        for name, bad in (("tokens per byte under", log_line(s, 11111, "0.0999", "0.3333")),
                          ("tokens per byte over", log_line(s, 11111, "1.0001", "0.3333")),
                          ("ratio under", log_line(s, 11111, "0.2222", "0.0999")),
                          ("ratio over", log_line(s, 11111, "0.2222", "1.0001")),
                          ("base at the target", log_line(s, 200_000, "0.2222", "0.3333")),
                          ("base above the target", log_line(s, 250_000, "0.2222", "0.3333"))):
            with self.subTest(name):
                self.write_log(log_line(s) + bad)
                self.assertEqual(self.counts(), self.GOOD)  # the line before it counts
                self.write_log(bad)
                self.assertEqual(self.counts(), self.NEW)

    def test_counts_at_their_bounds_are_read_and_the_target_is_the_objects_own(self):
        self.write_log(log_line(self.SESSION, 199_999, "0.1000", "1.0000"))
        self.assertEqual(self.counts(), (199_999, 0.1, 1.0))
        self.write_log(log_line(self.SESSION))
        self.assertEqual(self.counts(target=50_000), self.NEW)  # 62,899 is above a target of 50,000
        self.write_log(log_line(self.SESSION))
        self.assertEqual(self.counts(context=80_000), self.NEW)  # and above Hermes's threshold: 75% of 80,000
        self.write_log(log_line(self.SESSION))
        self.assertEqual(self.counts(context=100_000), self.GOOD)

    def test_the_older_log_file_is_read_when_the_newer_one_has_no_line_of_the_session(self):
        self.write_log(log_line(self.SESSION), "ace.log.1")
        self.assertEqual(self.counts(), self.GOOD)  # no ace.log yet: it turned over a moment ago
        self.write_log(log_line("s2", *self.OTHER))
        self.assertEqual(self.counts(session="s3"), self.NEW)
        self.write_log(log_line("s2", *self.OTHER))
        self.assertEqual(self.counts(), self.GOOD)

    def test_the_newest_line_of_the_session_that_has_counts_wins(self):
        s = self.SESSION
        self.write_log(log_line(s, *self.OTHER), "ace.log.1")
        self.write_log(log_line(s, *self.OTHER) + log_line(s) + log_line("s2", *self.OTHER) + log_line(s, "None", "0.3000", "0.3000"))
        self.assertEqual(self.counts(), self.GOOD)  # the last line is of a new object that had no count: base=None


ASK = {"role": "user", "content": "read every shelf and count the copper kettles"}


def step(s, command="cat shelf-%d", lines=60):
    """One step of the agent: a terminal call and its long result."""
    return [{"role": "assistant", "content": "", "tool_calls": [{"id": "w%d" % s, "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": command % s})}}]},
            {"role": "tool", "tool_call_id": "w%d" % s,
             "content": "".join("shelf %d row %d holds a copper kettle\n" % (s, i) for i in range(lines))}]


def call(name, **args):
    return {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class WorkTest(unittest.TestCase):
    """A tool call is work when it writes: a file, a commit, a skill, a helper's task. A read is no work."""

    def test_a_call_that_writes_is_work(self):
        for c in (call("write_file", path="a", content="x"), call("patch", path="a", old_string="x", new_string="y"),
                  call("mcp__write_file", path="a", content="x"), call("skill_manage", operations=[]),
                  call("delegate_task", tasks=[]), call("terminal", command="git commit -qam x && git push"),
                  call("terminal", command="git -C ~/w merge --no-ff b"), call("terminal", command="python3 x.py > out.txt 2>&1"),
                  call("terminal", command="echo a >> log"), call("terminal", command="sed -i 's/a/b/' f"),
                  call("terminal", command="mv a b"), call("terminal", command="cd w && rm -f x"),
                  call("terminal", command="mkdir -p d"), call("terminal", command="ffmpeg -y -i a.mp4 b.mp4"),
                  call("execute_code", code="open('a.txt', 'w').write('x')"), call("execute_code", code="p.write_text('x')")):
            with self.subTest(c["function"]["arguments"]):
                self.assertTrue(ace.is_work(c))

    def test_a_call_that_only_reads_is_no_work(self):
        for c in (call("read_file", path="a"), call("search_files", pattern="x"), call("crumb", id="x", level=1),
                  call("web_extract", urls=["u"]), call("terminal", command="cat a | grep b 2>&1 | head"),
                  call("terminal", command="ls -la > /dev/null"), call("terminal", command="git status --short; git show HEAD:a"),
                  call("terminal", command="sed -n 1,40p f; git log --oneline -5; git diff master...b"),
                  call("terminal", command="python3 -c 'print(1 if 2 > 1 else 0)'"),
                  call("terminal", command="python3 -m unittest discover -s tests 2>&1 | tail -3"),
                  call("execute_code", code="x = [a for a in b if a > c]\nprint(x)"), {"id": "c2"}):
            with self.subTest(str(c)):
                self.assertFalse(ace.is_work(c))


class RatchetTest(unittest.TestCase):
    """When the tool calls since the last fold did no work (they only read), the target grows by RATCHET
    (200,000 -> 290,000 -> 420,500) in place of a new fold: a read and fold loop shows that a fold keeps too
    little for the work. A target never grows past Hermes's threshold or the budget."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def engine(self, target=40_000, cfg=None):
        if cfg:
            os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"), exist_ok=True)
            with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        e = ace.Ace(target_tokens=target)
        e.on_session_start("s-ratchet", hermes_home=self.tmp.name)
        self.addCleanup(e.close)
        return e

    def wide(self, s, names=("read_file", "search_files", "web_extract", "browser_exec"), lines=60):
        """One step of the agent that reads with several different tools (no tool writes)."""
        calls = [{"id": "x%d-%d" % (s, i), "type": "function", "function": {"name": n, "arguments": json.dumps({"q": "shelf-%d" % s})}}
                 for i, n in enumerate(names)]
        return [{"role": "assistant", "content": "", "tool_calls": calls}] + [
            {"role": "tool", "tool_call_id": c["id"], "content": "".join("shelf %d row %d holds a copper kettle\n" % (s, i) for i in range(lines))}
            for c in calls]

    def acts(self):
        with open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log"), encoding="utf-8") as f:
            return re.findall(r" act=(\w+) ", f.read())

    def until(self, e, msgs, act, start, command="cat shelf-%d"):
        """Add one step per request until ACE logs act."""
        for s in range(start, start + 400):
            msgs = msgs + step(s, command)
            e.select_context(msgs, budget_tokens=1_000_000)
            if self.acts()[-1] == act:
                return msgs
        self.fail("no %s in 400 steps: %s" % (act, sorted(set(self.acts()))))

    def test_an_interval_that_only_reads_raises_the_target_in_place_of_a_fold(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        self.until(e, msgs, "ratchet", 1000)  # reads only: the request passes 40,000 again
        acts = self.acts()
        self.assertEqual(acts.count("fold"), 1)
        self.assertEqual((e.folds, e._target(1_000_000)), (1, 58_000))
        self.assertIn("target=58000 ", open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log")).read().split("\n")[-2] + " ")

    def test_another_interval_with_no_work_raises_it_again(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        self.until(e, msgs, "ratchet", 2000)  # the interval starts again at a raise
        self.assertEqual((e.folds, e._target(1_000_000)), (1, 84_100))

    def test_an_interval_with_work_folds_at_the_same_target(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = msgs + step(999, "git commit -qam 'shelf %d'")
        self.until(e, msgs, "fold", 1000)
        self.assertNotIn("ratchet", self.acts())
        self.assertEqual((e.folds, e._target(1_000_000)), (2, 40_000))

    def test_work_after_a_raise_tapers_the_target_back_by_the_fold_share(self):
        """Reading earned room above the band. Work is no reading: each interval of work takes 10% (ace_fold_to
        0.9) off the room above the band, a step at a time, down to the band. No fold is forced."""
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        self.assertEqual(e._target(1_000_000), 58_000)  # 40,000 x 1.45
        msgs = msgs + step(1999, "git commit -qam 'shelf %d'")  # work
        e.select_context(msgs, budget_tokens=1_000_000)
        self.assertEqual(e._target(1_000_000), 56_200)  # 40,000 + 90% of the 18,000 above the band
        seen = [e._target(1_000_000)]
        for s in range(2000, 2060):  # more work: the room above the band shrinks 10% an interval
            msgs = msgs + step(s, "git commit -qam 'shelf %d'")
            e.select_context(msgs, budget_tokens=1_000_000)
            seen.append(e._target(1_000_000))
        self.assertEqual(seen, sorted(seen, reverse=True))  # a taper: it never goes up while the work goes on
        self.assertEqual((e.ratchet, seen[-1]), (1.0, 40_000))
        self.assertEqual(self.acts().count("ratchet"), 1)

    def test_a_wide_set_of_tools_tapers_the_target_too(self):
        """Four different tools in an interval, none of them writing: the model is not looping on one read."""
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        e.select_context(msgs + self.wide(3000), budget_tokens=1_000_000)
        self.assertEqual(e._target(1_000_000), 56_200)

    def test_a_wide_set_of_tools_is_no_reason_to_taper_when_ace_diverse_is_zero(self):
        e = self.engine(cfg={"ace_diverse": 0})
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        for s in range(3000, 3030):
            msgs = msgs + self.wide(s)
            e.select_context(msgs, budget_tokens=1_000_000)
        self.assertGreater(e.ratchet, 1.0)

    def test_reading_with_one_tool_keeps_the_room(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        for s in range(1500, 1520):
            msgs = msgs + step(s)
            e.select_context(msgs, budget_tokens=1_000_000)
        self.assertGreater(e.ratchet, 1.0)

    def test_a_chat_with_no_tool_calls_folds_at_the_same_target(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        for t in range(400):
            msgs = msgs + [{"role": "user", "content": "line %d: %s" % (t, "a copper kettle " * 150)},
                           {"role": "assistant", "content": "Noted, line %d." % t}]
            e.select_context(msgs, budget_tokens=1_000_000)
            if e.folds == 2:
                break
        self.assertEqual(e.folds, 2)
        self.assertNotIn("ratchet", self.acts())

    def test_the_target_never_grows_past_hermes_threshold(self):
        e = ace.Ace(target_tokens=40_000)
        e.update_model("a-model", 100_000)  # Hermes's threshold: 75% of the context, 75,000
        seen = []
        while len(seen) < 9 and e._raise(1_000_000):
            seen.append(e._target(1_000_000))
        self.assertEqual(seen, [58_000, 75_000])
        self.assertEqual(e._target(60_000), 60_000)  # and never past the budget

    def test_a_new_object_keeps_the_raised_target(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        self.until(e, msgs, "ratchet", 1000)
        e.close()
        two = self.engine()
        two.select_context([SYSTEM, ASK] + step(0), budget_tokens=1_000_000)
        self.assertEqual(two._target(1_000_000), 58_000)
        other = ace.Ace(target_tokens=40_000)
        other.on_session_start("s-other", hermes_home=self.tmp.name)
        self.addCleanup(other.close)
        other.select_context([SYSTEM, ASK] + step(0), budget_tokens=1_000_000)
        self.assertEqual(other._target(1_000_000), 40_000)  # another session starts at its own target


class LandingTest(unittest.TestCase):
    """A fold lands on its aim (FOLD_TO of the target) by the model's own count. The layer costs fewer tokens
    per byte than the messages that it folds, so a fold by the count of the request before it lands under
    its aim (ace.log, Nov 9 2026: 60 folds in a row, a median of 0.709). The next fold divides its aim by the
    gain of the fold before: the model's count of the folded request over the estimate that the fold aimed at."""
    FIX, RAW, LAYER = 4_000, 0.45, 0.25  # the model: a fixed part, and the tokens per byte of each kind of text

    def count(self, e, out, sent):
        layer = len(e.layer.encode("utf-8")) if out is not None else 0
        return self.FIX + math.ceil(self.RAW * (ace.request_bytes(sent) - layer) + self.LAYER * layer)

    def run_folds(self, home, folds=4):
        e = ace.Ace(target_tokens=40_000)
        e.on_session_start("s-land", hermes_home=home)
        self.addCleanup(e.close)
        msgs, landed = [SYSTEM, ASK], []
        for s in range(3000):
            msgs = msgs + step(s, "cp shelf-%d copies")  # work: no ratchet
            before = e.folds
            out = e.select_context(msgs, budget_tokens=1_000_000)
            n = self.count(e, out, msgs if out is None else out)
            if e.folds > before:
                landed.append(n / (ace.FOLD_TO * 40_000))
            e.update_from_response({"prompt_tokens": n})
            if len(landed) == folds:
                e.select_context(msgs + step(s + 1), budget_tokens=1_000_000)  # its log line holds the new gain
                return e, landed
        self.fail("%d folds in 3000 steps" % len(landed))

    def test_each_fold_after_the_first_lands_on_its_aim(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e, landed = self.run_folds(tmp.name)
        for x in landed[1:]:  # within 10%: the live log gave q1 0.911 and q3 1.094; the old code lands near 0.79
            self.assertAlmostEqual(x, 1.0, delta=0.1, msg=landed)
        self.assertLess(e.gain, 0.95)

    def test_a_new_object_reads_the_gain_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e, _ = self.run_folds(tmp.name, folds=2)
        e.close()
        two = ace.Ace(target_tokens=40_000)
        two.on_session_start("s-land", hermes_home=tmp.name)
        self.addCleanup(two.close)
        two.select_context([SYSTEM, ASK] + step(0), budget_tokens=1_000_000)
        self.assertEqual(round(two.gain, 4), round(e.gain, 4))


class ChildTest(unittest.TestCase):
    """A delegate_task child has no memory provider, so no crumb tool; folded, it got a layer that it could not open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def engine(self, platform):
        e = ace.Ace(target_tokens=16000)
        e.on_session_start("s-%s" % platform, hermes_home=self.tmp.name, platform=platform)
        self.addCleanup(e.close)
        return e

    def lines(self):
        with open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log")) as f:
            return f.read().splitlines()

    def test_a_child_with_no_crumb_tool_is_not_folded(self):
        child = self.engine("subagent")
        req = [SYSTEM] + chat(40)  # far over the target; the prompt names no crumb tool
        for _ in range(2):  # and the same on the next request: no state of a fold builds up
            self.assertIsNone(child.select_context(req, budget_tokens=1_000_000))
        lines = self.lines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn(" act=pass ", line)
            self.assertIn(" folds=0 ", line)
        self.assertEqual((child.folds, child.layer), (0, ""))

    def test_the_same_request_folds_where_the_model_has_the_tool(self):
        from breadcrumbs import hermes
        self.assertIn(ace.BLOCK_NAMES_CRUMB, hermes.SYSTEM_BLOCK)  # the line that the guard looks for is the provider's
        req = [SYSTEM] + chat(40)
        self.assertIsNotNone(self.engine("cli").select_context(req, budget_tokens=1_000_000))  # a primary agent
        self.assertIsNotNone(self.engine("tui").select_context(req[1:], budget_tokens=1_000_000))  # any prompt, even none
        named = [{"role": "system", "content": hermes.SYSTEM_BLOCK}] + req[1:]  # a child that has the provider
        self.assertIsNotNone(self.engine("subagent").select_context(named, budget_tokens=1_000_000))
        self.assertEqual(sum(" act=fold " in x for x in self.lines()), 3)

    def test_a_child_whose_request_has_no_system_message_names_no_crumb_tool(self):
        self.assertIsNone(self.engine("subagent").select_context(chat(40), budget_tokens=1_000_000))

    def test_a_child_stays_a_child_when_a_later_session_start_names_no_platform_or_another(self):
        child = self.engine("subagent")
        for kwargs in ({}, {"platform": "tui"}):  # run_agent names the session source, which can be the parent's
            child.on_session_start("s-next", hermes_home=self.tmp.name, boundary_reason="compression", **kwargs)
            self.assertIsNone(child.select_context([SYSTEM] + chat(40), budget_tokens=1_000_000))


if __name__ == "__main__":
    unittest.main()
