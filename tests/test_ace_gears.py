"""ACE gears: the log fields (B1), the memory tools that are no work (B2), and the gearbox (B4 to B7).

The gearbox is a lockup for research and reasoning and a low gear for work (breadcrumbs/ace.py, "The gears"). Each
dial of it is off by default, and a request is then byte for byte what it was before the gears.
"""
import json
import os
import re
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import ace, view

from test_ace import ASK, SYSTEM, call, chat, long_turn, step

FIXED = 62_899


def log_line(sess, tail="", base=FIXED, tpb="0.3038", r="0.4777"):
    """One whole line of ace.log. tail: the fields after layer= (an old line has none)."""
    return ("2026-01-02T03:04:05 pid=7 sess=%s act=keep in=300000 out=110000 last=170000 base=%s tpb=%s r=%s "
            "folds=1 layer=40%s\n" % (sess, base, tpb, r, tail))


class Kit(unittest.TestCase):
    """An engine in a scratch home, and the helpers that read its ace.log."""
    SESSION = "local-0123456789abcdef01"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def engine(self, target=40_000, cfg=None, session="s-gear", context=0):
        if cfg is not None:
            os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"), exist_ok=True)
            with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        e = ace.Ace(target_tokens=target)
        e.on_session_start(session, hermes_home=self.tmp.name)
        if context:
            e.update_model("a-model", context)
        self.addCleanup(e.close)
        return e

    def lines(self):
        with open(os.path.join(self.tmp.name, "breadcrumbs", "ace.log"), encoding="utf-8") as f:
            return f.read().splitlines()

    def acts(self):
        return [m.group(1) for m in (re.search(r" act=(\w+) ", x) for x in self.lines()) if m]

    def field(self, key, n=-1):
        m = re.search(r" %s=(\S+)" % key, self.lines()[n])
        self.assertIsNotNone(m, "no %s in %s" % (key, self.lines()[n]))
        return m.group(1) if m else ""

    def write_log(self, text, name="ace.log"):
        root = os.path.join(self.tmp.name, "breadcrumbs")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def ask(self, e, msgs):
        return e.select_context(msgs, budget_tokens=1_000_000)

    def until(self, e, msgs, act, start, command="cat shelf-%d"):
        """Add one step per request until ACE logs act."""
        for s in range(start, start + 400):
            msgs = msgs + step(s, command)
            self.ask(e, msgs)
            if self.acts()[-1] == act:
                return msgs
        self.fail("no %s in 400 steps: %s" % (act, sorted(set(self.acts()))))

    def wide(self, s, names=("read_file", "search_files", "web_extract", "browser_exec"), lines=60):
        """One step of the agent that reads with several different tools (no tool writes)."""
        calls = [{"id": "x%d-%d" % (s, i), "type": "function", "function": {"name": n, "arguments": json.dumps({"q": "shelf-%d" % s})}}
                 for i, n in enumerate(names)]
        return [{"role": "assistant", "content": "", "tool_calls": calls}] + [
            {"role": "tool", "tool_call_id": c["id"], "content": "".join("shelf %d row %d holds a copper kettle\n" % (s, i) for i in range(lines))}
            for c in calls]

    def turn(self, e, msgs, t, steps=3, work=0, lines=60):
        """One user turn: a question, then `steps` steps that read (the first `work` of them write), then an answer.
        Every request of the turn goes through ACE. Returns the messages and one dict for each request."""
        seen = []
        msgs = msgs + [{"role": "user", "content": "turn %d: where is the walrus kettle number %d?" % (t, t)}]
        for s in range(steps + 1):
            out = self.ask(e, msgs)
            seen.append({"first": s == 0, "act": self.acts()[-1], "gear": e.gear, "target": e._target(1_000_000),
                         "layer": e.layer if out is not None else None, "sent": e.tokens(ace.request_bytes(out if out is not None else msgs))})
            if s < steps:
                msgs = msgs + step(t * 100 + s, "git commit -qam 'shelf %d'" if s < work else "cat shelf-%d", lines)
        return msgs + [{"role": "assistant", "content": "Kettle %d is on shelf %d." % (t, t)}], seen


class LogFieldsTest(Kit):
    """B1: each line of ace.log ends with gear, think, calls, work and room (after lay=)."""

    def test_a_line_ends_with_the_gear_the_thinking_the_new_calls_the_work_flag_and_the_room(self):
        e = self.engine(target=16_000)
        self.ask(e, [SYSTEM] + chat(3))
        line = self.lines()[0]
        self.assertRegex(line, r" lay=\d+ gear=- think=0 calls=3 work=0 room=0$")  # three reads, no gearbox
        self.assertTrue(ace.LOG_RE.fullmatch(line), line)

    def test_think_is_the_thinking_of_the_request_in_tokens_and_counted_once(self):
        e = self.engine(target=400_000)
        msgs = [SYSTEM, ASK] + long_turn(5, think=200, ask=False)[:]  # 5 steps of 1,400 characters of thinking
        self.ask(e, msgs)
        want = int(e.tpb * 5 * 1400)
        self.assertEqual(int(self.field("think")), want)
        self.assertEqual(want, 2100)
        stored = [dict(m, reasoning_details=json.dumps(m["reasoning_details"])) if "reasoning_details" in m else m for m in msgs]
        self.ask(e, stored)  # state.db keeps the details as JSON text: the same count
        self.assertEqual(int(self.field("think")), want)
        twice = [dict(m, reasoning="walrus " * 200, reasoning_content="walrus " * 200) if "reasoning_details" in m else m for m in msgs]
        self.ask(e, twice)  # three fields hold one thinking: it counts one time
        self.assertEqual(int(self.field("think")), want)

    def test_calls_counts_the_tool_calls_since_the_last_fold_or_raise_and_work_says_if_one_writes(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        fold_calls = int(self.field("calls"))
        self.assertGreater(fold_calls, 5)  # the interval that ended in this fold: all its calls
        self.assertEqual(self.field("work"), "0")
        msgs = msgs + step(900)
        self.ask(e, msgs)
        self.assertEqual((self.acts()[-1], self.field("calls"), self.field("work")), ("keep", "1", "0"))
        msgs = msgs + step(901, "git commit -qam 'shelf %d'")
        self.ask(e, msgs)
        self.assertEqual((self.field("calls"), self.field("work")), ("2", "1"))

    def test_a_request_with_no_gearbox_logs_no_room(self):
        e = self.engine()
        self.until(e, [SYSTEM, ASK], "fold", 0)
        self.assertEqual((self.field("gear"), self.field("room")), ("-", "0"))

    def test_a_line_with_the_new_fields_is_read_back_like_a_line_without_them(self):
        old = log_line(self.SESSION)
        new = log_line(self.SESSION, " target=58000 gain=0.7090 lay=5000 gear=lock think=1200 calls=3 work=0 room=500000")
        for name, text in (("old", old), ("new", new), ("both", old + new), ("both, new first", new + old)):
            with self.subTest(name):
                self.write_log(text)
                self.assertEqual(ace.logged_counts(os.path.join(self.tmp.name, "breadcrumbs"), self.SESSION, 200_000),
                                 (FIXED, 0.3038, 0.4777))

    def test_a_line_with_the_new_fields_gives_its_raised_target_and_gain_back(self):
        root = os.path.join(self.tmp.name, "breadcrumbs")
        self.write_log(log_line(self.SESSION, " target=58000 gain=0.7090"))
        self.assertEqual(ace.logged_ratchet(root, self.SESSION), (58_000, 0.709))
        self.write_log(log_line(self.SESSION, " target=58000 gain=0.7090 lay=5000"))
        self.assertEqual(ace.logged_ratchet(root, self.SESSION), (58_000, 0.709))
        self.write_log(log_line(self.SESSION, " target=290000 gain=0.8000 lay=5000 gear=low think=0 calls=0 work=1 room=500000"))
        self.assertEqual(ace.logged_ratchet(root, self.SESSION), (290_000, 0.8))

    def test_a_line_with_a_damaged_gear_field_is_ignored(self):
        root = os.path.join(self.tmp.name, "breadcrumbs")
        good = log_line(self.SESSION, " target=58000 gain=0.7090 lay=5000 gear=lock think=0 calls=0 work=0 room=0")
        for name, bad in (("a gear that is none", " target=1 gain=0.7090 lay=5 gear=sideways think=0 calls=0 work=0 room=0"),
                          ("a flag that is no flag", " target=1 gain=0.7090 lay=5 gear=low think=0 calls=0 work=2 room=0"),
                          ("a field missing", " target=1 gain=0.7090 lay=5 gear=low think=0 calls=0 work=1"),
                          ("a field too many", " target=1 gain=0.7090 lay=5 gear=low think=0 calls=0 work=1 room=0 x=1")):
            with self.subTest(name):
                self.write_log(good + bad)
                self.assertEqual(ace.logged_ratchet(root, self.SESSION), (58_000, 0.709))

    def test_the_status_lines_print_the_gear_when_the_line_has_one(self):
        self.write_log(log_line(self.SESSION, " target=200000 gain=1.0000 lay=5 gear=low think=9 calls=2 work=1 room=500000")
                       + log_line("local-fedcba9876543210ff", " target=200000 gain=1.0000"))
        out = ace.status_lines(os.path.join(self.tmp.name, "breadcrumbs"))
        self.assertEqual(len(out), 2)
        mine = next(x for x in out if self.SESSION in x)
        other = next(x for x in out if "fedcba" in x)
        self.assertIn("gear low", mine)
        self.assertNotIn("gear", other)


class ViewLogTest(Kit):
    """B1: the explorer reads the new keys (view.ace_requests) and keeps them in the points of the Size panel."""

    def test_the_new_keys_reach_the_requests_and_the_points(self):
        self.write_log(
            log_line(self.SESSION, " target=200000 gain=1.0000 lay=5 gear=lock think=9 calls=2 work=0 room=500000")
            + log_line(self.SESSION, " target=200000 gain=1.0000 lay=5 gear=low think=10 calls=3 work=1 room=500000")
            + log_line(self.SESSION, " target=200000 gain=1.0000 lay=5"))
        rows = view.ace_requests(os.path.join(self.tmp.name, "breadcrumbs"))
        self.assertEqual([(r["gear"], r["think"], r["calls"], r["work"], r["room"]) for r in rows],
                         [("lock", 9, 2, 0, 500_000), ("low", 10, 3, 1, 500_000), (None, None, None, None, None)])
        pts = view.ace_series(rows)
        self.assertEqual([(p["gear"], p["think"], p["calls"], p["work"], p["room"]) for p in pts],
                         [("lock", 9, 2, 0, 500_000), ("low", 10, 3, 1, 500_000), (None, None, None, None, None)])


class MemoryToolsTest(Kit):
    """B2: opening a crumb is no work. ACE asks the model to open crumbs after a fold; the two crumb tools must not
    count as two of the ace_diverse different tools (a read, a search, crumb and crumb_find made 4: work)."""

    def raised(self, session="s-gear"):
        e = self.engine(session=session)
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = self.until(e, msgs, "ratchet", 1000)
        self.assertEqual(e._target(1_000_000), 58_000)
        return e, msgs

    def test_a_read_a_search_and_the_two_crumb_tools_are_no_work(self):
        for i, names in enumerate((("read_file", "search_files", "crumb", "crumb_find"),
                                   ("read_file", "search_files", "mcp__crumb", "mcp__crumb_find"))):
            with self.subTest(names):
                e, msgs = self.raised("s-memory-%d" % i)  # each variant is a chat of its own
                msgs = msgs + self.wide(3000, names)
                self.ask(e, msgs)
                self.assertFalse(e._busy(ace.split(msgs)[1]))
                self.assertEqual(e._target(1_000_000), 58_000)  # no taper: the room stays
                self.assertGreater(e.ratchet, 1.0)

    def test_three_other_tools_and_a_crumb_call_are_still_reading_and_four_other_tools_are_work(self):
        e, msgs = self.raised()
        three = msgs + self.wide(3000, ("read_file", "search_files", "web_extract", "mcp__crumb"))
        self.ask(e, three)
        self.assertEqual(e._target(1_000_000), 58_000)
        four = three + self.wide(3001, ("read_file", "search_files", "web_extract", "browser_exec", "mcp__crumb"))
        self.ask(e, four)
        self.assertEqual(e._target(1_000_000), 56_200)  # four tools that are no memory tools: work, as before

    def test_a_crumb_call_that_is_the_only_new_call_makes_the_interval_idle(self):
        e = self.engine()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        msgs = msgs + self.wide(900, ("mcp__crumb",)) + self.wide(901, ("mcp__crumb_find",))
        turns = ace.split(msgs)[1]
        self.assertTrue(e._idle(turns))  # reads only: a read and fold loop, so the target may rise


class LockupTest(Kit):
    """B4 and B5: ace_lockup and ace_room. From the first request the target is the research room, not the band.
    Nothing folds below the room: thinking and reads keep the lockup. At the room a fold is the gentle one."""
    BAND, ROOM = 40_000, 120_000

    def lock(self, session="s-lock", **more):
        return self.engine(target=self.BAND, cfg=dict({"ace_lockup": True, "ace_room": self.ROOM}, **more), session=session)

    def talk(self, t):
        """A turn with no tool call."""
        return [{"role": "user", "content": "line %d: %s" % (t, "a copper kettle " * 150)},
                {"role": "assistant", "content": "Noted, line %d." % t}]

    def think(self, t):
        """A turn of thinking and no tool call."""
        return [{"role": "user", "content": "think %d" % t},
                {"role": "assistant", "content": "ok %d" % t,
                 "reasoning_details": [{"type": "thinking", "thinking": "a copper kettle " * 150, "signature": "x" * 400}]}]

    def grow(self, e, add):
        """Add turns until ACE folds. Returns the messages before the fold, the messages of the fold, what ACE sent
        for them, and the largest request (in tokens) that went out before the fold."""
        msgs, biggest = [SYSTEM], 0
        for t in range(3000):
            before, msgs = msgs, msgs + add(t)
            out = self.ask(e, msgs)
            if self.acts()[-1] == "fold":
                return before, msgs, out, biggest
            biggest = max(biggest, e.tokens(ace.request_bytes(msgs if out is None else out)))
        self.fail("no fold in 3000 turns: %s" % sorted(set(self.acts())))

    def test_a_chat_with_no_call_passes_the_band_and_folds_at_the_room(self):
        e = self.lock()
        before, msgs, out, biggest = self.grow(e, self.talk)
        self.assertGreater(biggest, self.BAND)  # it passed the band, and nothing folded
        self.assertLessEqual(e.tokens(ace.request_bytes(before)), self.ROOM)
        self.assertGreater(e.tokens(ace.request_bytes(msgs)), self.ROOM)  # the first fold: the request is over the room
        self.assertEqual(self.acts().count("fold"), 1)
        self.assertEqual(e.gear, "lock")

    def test_a_chat_of_thinking_alone_does_the_same(self):
        e = self.lock()
        before, msgs, out, biggest = self.grow(e, self.think)
        self.assertGreater(biggest, self.BAND)
        self.assertGreater(e.tokens(ace.request_bytes(msgs)), self.ROOM)
        self.assertEqual({self.field("gear", n) for n in range(len(self.lines()))}, {"lock"})

    def test_without_the_lockup_the_same_chat_folds_at_the_band(self):
        e = self.engine(target=self.BAND)
        before, msgs, out, biggest = self.grow(e, self.talk)
        self.assertLessEqual(biggest, self.BAND)
        self.assertGreater(e.tokens(ace.request_bytes(msgs)), self.BAND)
        self.assertLess(e.tokens(ace.request_bytes(msgs)), self.ROOM)

    def test_a_chat_that_only_reads_does_not_fold_below_the_room(self):
        e = self.lock()
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        self.assertGreater(e.tokens(ace.request_bytes(msgs)), self.ROOM)
        self.assertEqual(self.acts().count("fold"), 1)
        self.assertEqual({self.field("gear", n) for n in range(len(self.lines()))}, {"lock"})
        self.assertEqual({self.field("work", n) for n in range(len(self.lines()))}, {"0"})
        self.assertEqual({self.field("room", n) for n in range(len(self.lines()))}, {str(self.ROOM)})

    def test_the_crumb_tools_keep_the_lockup_too(self):
        e = self.lock()
        msgs = self.until(e, [SYSTEM, ASK], "pass", 0)
        for s in range(3000, 3040):
            msgs = msgs + self.wide(s, ("read_file", "search_files", "mcp__crumb", "mcp__crumb_find", "web_extract", "browser_exec"))
            self.ask(e, msgs)
        self.assertEqual(e.gear, "lock")  # more than four tools, but none of them writes

    def test_at_the_room_the_fold_is_the_gentle_one_and_leaves_ninety_percent_of_it(self):
        e = self.lock()
        before, msgs, out, biggest = self.grow(e, self.talk)
        sent = e.tokens(ace.request_bytes(out))
        self.assertLessEqual(sent, ace.FOLD_TO * self.ROOM)
        self.assertGreater(sent, 0.5 * self.ROOM)

    def test_the_room_is_a_grant_and_not_a_cap_the_chat_grows_back_to_it_and_folds_again(self):
        e = self.lock()
        before, msgs, out, biggest = self.grow(e, self.talk)
        for t in range(1000, 4000):
            msgs = msgs + self.talk(t)
            self.ask(e, msgs)
            if self.acts().count("fold") == 2:
                break
        self.assertEqual(self.acts().count("fold"), 2)
        folds = [n for n, a in enumerate(self.acts()) if a == "fold"]
        between = [int(self.field("out", n)) for n in range(folds[0] + 1, folds[1])]
        self.assertTrue(0.97 * self.ROOM < max(between) <= self.ROOM, max(between))  # it grew back to the room, then folded
        self.assertEqual(e._target(1_000_000), self.ROOM)

    def test_the_first_target_is_the_room_in_one_step_and_not_a_ratchet_of_1_45(self):
        e = self.lock()
        self.ask(e, [SYSTEM] + chat(2))
        self.assertEqual(e._target(1_000_000), self.ROOM)
        self.assertEqual(self.field("target"), str(self.ROOM))
        self.assertEqual(self.acts(), ["pass"])

    def test_a_room_of_zero_is_hermes_threshold_and_a_larger_room_is_cut_to_it(self):
        for room, want in ((0, 75_000), (500_000, 75_000), (60_000, 60_000)):
            with self.subTest(room=room):
                e = self.engine(target=self.BAND, cfg={"ace_lockup": True, "ace_room": room}, session="s-room-%d" % room, context=100_000)
                self.assertEqual(e.threshold_tokens, 75_000)  # 75% of the context
                e.select_context([SYSTEM] + chat(2), budget_tokens=100_000)
                self.assertEqual(e._target(100_000), want)
                self.assertEqual(e.room_now, want)

    def test_a_room_under_the_band_is_the_band(self):
        e = self.engine(target=self.BAND, cfg={"ace_lockup": True, "ace_room": 20_000})
        self.ask(e, [SYSTEM] + chat(2))
        self.assertEqual(e._target(1_000_000), self.BAND)
        self.assertEqual((e.room_now, self.field("room")), (self.BAND, str(self.BAND)))  # the room that the log keeps, too

    def test_with_no_room_known_idle_counts_before_the_first_fold_and_with_no_new_call(self):
        """No room and no threshold (a model whose context length is not known): the target grows by the ratchet, as
        before, but a chat with no call at all is idle at once, also before its first fold."""
        e = self.engine(target=self.BAND, cfg={"ace_lockup": True})
        msgs = [SYSTEM]
        for t in range(3000):
            msgs = msgs + self.talk(t)
            e.select_context(msgs)  # budget_tokens: unknown
            if self.acts()[-1] != "pass":
                break
        self.assertEqual(self.acts()[-1], "ratchet")  # without the lockup the first overflow always folds
        self.assertEqual(self.acts().count("fold"), 0)
        self.assertEqual((e.room_now, self.field("room")), (0, "0"))

    def test_the_first_call_that_writes_shifts_down_and_the_log_says_so(self):
        e = self.lock()
        msgs = self.until(e, [SYSTEM, ASK], "pass", 0)
        self.assertEqual((e.gear, self.field("gear")), ("lock", "lock"))
        msgs = msgs + step(900, "git commit -qam 'shelf %d'")
        self.ask(e, msgs)
        self.assertEqual((e.gear, self.field("gear"), self.field("work")), ("low", "low", "1"))

    def test_a_new_engine_object_reads_the_gear_and_the_room_back_from_the_log(self):
        s = "local-0123456789abcdef01"
        for gear, want in (("low", "low"), ("lock", "lock"), ("-", "lock")):
            with self.subTest(gear):
                self.write_log(log_line(s, " target=60000 gain=1.0000 lay=0 gear=%s think=0 calls=0 work=0 room=%d" % (gear, 60_000)))
                e = ace.Ace(target_tokens=self.BAND)
                os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"), exist_ok=True)
                with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w", encoding="utf-8") as f:
                    json.dump({"ace_lockup": True, "ace_room": 0}, f)  # no room of its own, and no threshold: the log has it
                e.on_session_start(s, hermes_home=self.tmp.name)
                self.addCleanup(e.close)
                e.select_context([SYSTEM] + chat(2), budget_tokens=0)
                self.assertEqual(e.gear, want)
                self.assertEqual(e.room_now, 60_000)  # read back from the log

    def test_a_chat_resumed_in_lockup_starts_its_interval_at_its_first_request(self):
        """The writes of a chat that was before the object count for nothing: the chat is in the gear that its log says."""
        s = "local-0123456789abcdef01"
        self.write_log(log_line(s, " target=40000 gain=1.0000 lay=0 gear=lock think=0 calls=0 work=0 room=%d" % self.ROOM))
        e = self.lock(session=s)
        self.ask(e, [SYSTEM] + chat(2) + step(7, "git commit -qam 'shelf %d'"))
        self.assertEqual(e.gear, "lock")
        self.ask(e, [SYSTEM] + chat(2) + step(7, "git commit -qam 'shelf %d'") + step(8, "git commit -qam 'shelf %d'"))
        self.assertEqual(e.gear, "low")  # a write that the object saw


    def test_a_room_that_is_set_now_wins_over_the_room_that_the_log_kept(self):
        s = "local-0123456789abcdef01"
        self.write_log(log_line(s, " target=60000 gain=1.0000 lay=0 gear=lock think=0 calls=0 work=0 room=60000"))
        e = self.lock(session=s)
        self.ask(e, [SYSTEM] + chat(2))
        self.assertEqual((e.room_now, self.field("room")), (self.ROOM, str(self.ROOM)))  # the dial counts, then the log

    def test_a_new_chat_whose_first_request_holds_a_write_is_in_low_gear_at_once(self):
        """The log does not know this chat, so its interval is from its start: what its first request holds counts."""
        e = self.lock()
        self.ask(e, [SYSTEM, ASK] + step(0, "git commit -qam 'shelf %d'"))
        self.assertEqual((e.gear, self.field("gear"), self.field("calls"), self.field("work")), ("low", "low", "1", "1"))

    def test_in_low_gear_reads_after_a_fold_fold_again_and_do_not_raise_the_target(self):
        """No room is known, so the target is the band. A write shifts down. The reads after the first fold are no
        reason to raise the target in low gear: the raise is for the gear of reading."""
        e = self.engine(target=self.BAND, cfg={"ace_lockup": True})
        msgs = [SYSTEM, ASK] + step(0, "git commit -qam 'shelf %d'")
        for s in range(1, 600):
            msgs = msgs + step(s)
            e.select_context(msgs)  # budget_tokens: unknown
            if self.acts().count("fold") == 2:
                break
        self.assertEqual(self.acts().count("fold"), 2)
        self.assertNotIn("ratchet", self.acts())
        self.assertEqual({self.field("gear", n) for n in range(len(self.lines()))}, {"low"})

    def test_the_gear_holds_when_the_model_changes(self):
        e = self.lock()
        self.ask(e, [SYSTEM, ASK] + step(0, "git commit -qam 'shelf %d'"))
        self.assertEqual(e.gear, "low")
        e.update_model("another-model", 1_000_000)  # a model switch reads the dials again
        self.assertEqual((e.gear, e.lockup, e.room), ("low", True, self.ROOM))

    def test_the_status_says_the_gear_and_the_room(self):
        e = self.lock()
        msgs = [SYSTEM] + chat(2)
        self.ask(e, msgs)
        self.assertEqual({k: v for k, v in e.get_status().items() if k in ("ace_gear", "ace_room_tokens")},
                         {"ace_gear": "lock", "ace_room_tokens": self.ROOM})
        self.ask(e, msgs + step(9, "git commit -qam 'shelf %d'"))
        self.assertEqual(e.get_status()["ace_gear"], "low")
        plain = self.engine(session="s-plain", cfg={})  # no gearbox (an empty config.json in the same home)
        self.ask(plain, msgs)
        self.assertEqual({k: v for k, v in plain.get_status().items() if k in ("ace_gear", "ace_room_tokens")},
                         {"ace_gear": "-", "ace_room_tokens": 0})

    def test_a_fold_that_hermes_asks_for_goes_to_the_room_too(self):
        """compress() is the fold at Hermes's own threshold. A new object has seen no request before it."""
        e = self.lock()
        msgs, t = [SYSTEM], 0
        while e.tokens(ace.request_bytes(msgs)) < 1.3 * self.ROOM:
            msgs, t = msgs + self.talk(t), t + 1
        out = e.compress(msgs, current_tokens=int(e.tokens(ace.request_bytes(msgs))), force=True)
        sent = e.tokens(ace.request_bytes(out))
        self.assertTrue(0.5 * self.ROOM < sent <= ace.FOLD_TO * self.ROOM, sent)  # and not 90% of the band
        self.assertEqual((e.gear, e.room_now), ("lock", self.ROOM))

    def test_an_object_that_starts_inside_a_turn_is_not_at_its_start(self):
        """A new object (a one-shot turn) is at the start of a turn when its newest turn holds the question alone."""
        for msgs, want in (([SYSTEM, ASK], True), ([SYSTEM, ASK] + step(0), False), ([SYSTEM] + chat(2), False)):
            with self.subTest(len(msgs)):
                e = self.lock(session="s-turn-%d" % len(msgs))
                turns = ace.split(msgs)[1]
                self.assertEqual(e._turn(turns), want)
                self.assertEqual(e._turn(turns), False)  # the same request again: no new turn


class DownshiftTest(Kit):
    """B6: ace_downshift (the share of the room above the band that each user turn of work takes off) and
    ace_turn_fold (in low gear the first request of a user turn folds again, and the layer holds inside the turn)."""
    BAND, ROOM = 40_000, 120_000

    def gears(self, session="s-down", **more):
        cfg = dict({"ace_lockup": True, "ace_room": self.ROOM, "ace_downshift": 0.5}, **more)
        return self.engine(target=self.BAND, cfg=cfg, session=session)

    def test_each_user_turn_of_work_takes_the_downshift_of_the_room_above_the_band(self):
        e = self.gears()
        msgs, seen = self.turn(e, [SYSTEM], 0, steps=2)  # a turn that only reads
        self.assertEqual({(x["gear"], x["target"]) for x in seen}, {("lock", self.ROOM)})
        msgs, seen = self.turn(e, msgs, 1, steps=3, work=1)  # the first write shifts down, and the target stays inside the turn
        self.assertEqual([x["gear"] for x in seen], ["lock", "low", "low", "low"])  # no request sees the write before it is made
        self.assertEqual({x["target"] for x in seen}, {self.ROOM})
        targets = []
        for t in range(2, 8):  # a turn of work: half of the room above the band goes (80,000, then 40,000, 20,000, ...)
            msgs, seen = self.turn(e, msgs, t, steps=2, work=1)
            self.assertEqual({x["target"] for x in seen}, {seen[0]["target"]})  # the target holds inside the turn
            targets.append(seen[0]["target"])
        self.assertEqual(targets, [80_000, 60_000, 50_000, 45_000, 42_500, 41_250])
        self.assertEqual(self.field("room"), str(self.ROOM))  # the log keeps the grant

    def test_a_turn_with_no_work_puts_the_chat_in_lockup_again(self):
        e = self.gears()
        msgs, _ = self.turn(e, [SYSTEM], 0, steps=2, work=1)
        msgs, seen = self.turn(e, msgs, 1, steps=2)  # the turn before had work: a share of the room goes
        self.assertEqual((seen[0]["gear"], seen[0]["target"]), ("low", 80_000))
        msgs, seen = self.turn(e, msgs, 2, steps=2)  # the turn before had none: lockup, and the room is back in one step
        self.assertEqual([(x["gear"], x["target"]) for x in seen], [("lock", self.ROOM)] * 3)
        msgs, seen = self.turn(e, msgs, 3, steps=2, work=1)  # and a write shifts down again
        self.assertEqual([x["gear"] for x in seen], ["lock", "low", "low"])

    def test_a_turn_with_no_call_at_all_puts_the_chat_in_lockup_again(self):
        e = self.gears()
        msgs, _ = self.turn(e, [SYSTEM], 0, steps=2, work=1)
        msgs, seen = self.turn(e, msgs, 1, steps=0)
        self.assertEqual(seen[0]["gear"], "low")
        msgs, seen = self.turn(e, msgs, 2, steps=0)
        self.assertEqual((seen[0]["gear"], seen[0]["target"]), ("lock", self.ROOM))

    def test_the_downshift_also_sets_the_share_of_the_old_taper_without_the_lockup(self):
        for i, (cfg, want) in enumerate((({"ace_downshift": 0.5}, 49_000), ({}, 56_200), ({"ace_fold_to": 0.5}, 49_000),
                                         ({"ace_downshift": 1.0}, 40_000))):
            with self.subTest(cfg):
                e = self.engine(cfg=cfg, session="s-old-%d" % i)
                msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
                msgs = self.until(e, msgs, "ratchet", 1000)
                self.assertEqual(e._target(1_000_000), 58_000)
                self.ask(e, msgs + step(1999, "git commit -qam 'shelf %d'"))  # one interval of work
                self.assertEqual(e._target(1_000_000), want)  # 40,000 and the share of the 18,000 above the band that stays

    def layered(self, **more):
        """A chat in lockup with a layer (one long turn of reads, with its first fold at the room), then a turn
        that writes: low gear. Returns the engine and the messages."""
        e = self.gears(ace_room=60_000, **more)
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0)
        self.assertTrue(e.layer)
        msgs = msgs + [{"role": "assistant", "content": "done reading"}]
        msgs, _ = self.turn(e, msgs, 1, steps=2, work=1)
        self.assertEqual(e.gear, "low")
        return e, msgs

    def test_in_low_gear_the_first_request_of_a_turn_folds_again_and_the_layer_holds_inside_it(self):
        e, msgs = self.layered(ace_turn_fold=True, ace_downshift=0.1)
        layers = []
        for t in range(2, 5):
            msgs, seen = self.turn(e, msgs, t, steps=3, work=1, lines=15)
            self.assertEqual((seen[0]["first"], seen[0]["act"]), (True, "fold"))  # the layer follows the new task
            self.assertEqual({x["act"] for x in seen[1:]}, {"keep"})  # and holds inside the turn: the prompt cache holds
            self.assertEqual(len({x["layer"] for x in seen}), 1)
            layers.append(seen[0]["layer"])
        self.assertEqual(len(set(layers)), len(layers))  # a layer for each turn

    def test_without_ace_turn_fold_the_same_turns_do_not_fold(self):
        e, msgs = self.layered(ace_downshift=0.1)
        for t in range(2, 5):
            msgs, seen = self.turn(e, msgs, t, steps=3, work=1, lines=15)
            self.assertEqual({x["act"] for x in seen}, {"keep"}, seen[0]["target"])  # the request stays under the target

    def test_a_turn_fold_needs_a_layer_and_low_gear(self):
        e = self.gears(ace_turn_fold=True)  # no fold yet, so no layer
        msgs, _ = self.turn(e, [SYSTEM], 0, steps=2, work=1)
        msgs, seen = self.turn(e, msgs, 1, steps=2, work=1)
        self.assertEqual((e.gear, e.layer, seen[0]["act"]), ("low", "", "pass"))
        e = self.gears(session="s-no-layer", ace_turn_fold=True)  # more turns than a fold keeps, and still no layer
        msgs = [SYSTEM]
        for t in range(8):
            msgs, seen = self.turn(e, msgs, t, steps=1, work=1, lines=8)
        self.assertEqual((e.gear, e.layer, self.acts().count("fold")), ("low", "", 0))
        e = self.gears(session="s-lock-only", ace_turn_fold=True, ace_room=60_000)  # a layer, and lockup
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0) + [{"role": "assistant", "content": "done"}]
        msgs, seen = self.turn(e, msgs, 1, steps=2)
        self.assertEqual((e.gear, seen[0]["act"]), ("lock", "keep"))  # reads only: lockup keeps the layer
        e = self.engine(target=self.BAND, cfg={"ace_turn_fold": True}, session="s-no-gears")  # no lockup, no gear
        msgs = self.until(e, [SYSTEM, ASK], "fold", 0) + [{"role": "assistant", "content": "done"}]
        msgs, seen = self.turn(e, msgs, 1, steps=2, work=1)
        self.assertEqual((e.gear, [x["act"] for x in seen]), ("-", ["keep"] * 3))
        msgs, seen = self.turn(e, msgs, 2, steps=2, work=1)
        self.assertNotEqual(seen[0]["act"], "fold")

    def test_a_turn_fold_needs_a_layer_also_when_the_request_is_big(self):
        """The first fold is the one at the target. A chat in low gear that has no layer, at 70% of its target, does
        not fold at the start of a turn."""
        e = self.gears(session="s-big-no-layer", ace_turn_fold=True, ace_room=60_000, ace_downshift=0.1)
        msgs, s = [SYSTEM, ASK], 0
        while e.tokens(ace.request_bytes(msgs)) < 0.7 * 60_000:  # reads: lockup, and the room is 60,000
            msgs, s = msgs + step(s), s + 1
            self.ask(e, msgs)
        self.assertEqual((e.gear, e.layer, self.acts().count("fold")), ("lock", "", 0))
        msgs = msgs + step(s, "git commit -qam 'shelf %d'")  # a write: low gear
        self.ask(e, msgs)
        self.assertEqual((e.gear, e.layer), ("low", ""))
        msgs = msgs + [{"role": "assistant", "content": "done"}]
        for t in (1, 2):  # each new turn takes a share of the room, and the request is 70% of the target
            msgs, seen = self.turn(e, msgs, t, steps=1, work=1, lines=8)
            self.assertEqual((seen[0]["act"], e.layer), ("pass", ""))
        self.assertEqual(self.acts().count("fold"), 0)

    def test_a_request_that_is_sent_again_is_no_new_turn(self):
        e, msgs = self.layered(ace_turn_fold=True)
        msgs = msgs[:-1] if msgs[-1]["role"] == "assistant" and not msgs[-1].get("tool_calls") else msgs
        asked = msgs + [{"role": "user", "content": "one more question about kettles"}]
        self.ask(e, asked)
        folds = self.acts().count("fold")
        first = e.layer
        self.ask(e, asked)  # Hermes sends it again (a retry): the same layer, no second fold
        self.assertEqual((self.acts().count("fold"), e.layer, self.acts()[-1]), (folds, first, "keep"))


class PersistTest(Kit):
    """B7: ace_persist. The words that the tools use and the words that stay from the early turns keep their crumbs
    (the verbs and nouns of the tool calls, and the terms that come back in later groups of turns)."""

    def setUp(self):
        super().setUp()
        small, ace.SMALL_TARGET = ace.SMALL_TARGET, 0  # the shares of a large target, as LargeShareLayerTest
        self.addCleanup(setattr, ace, "SMALL_TARGET", small)

    def chat(self, turns, mentions=(), tool_lines=90):
        """A chat whose turn 1 holds a porcelain teapot and whose turn 2 holds a tin funnel. The user messages of the
        turns in mentions say 'porcelain teapot' too. The new message at the end names neither."""
        req = [SYSTEM] + chat(turns, tool_lines)
        req[7]["content"] = "".join("shelf 1 row %d holds the porcelain teapot\n" % i for i in range(90))
        req[11]["content"] = "".join("shelf 2 row %d holds the tin funnel\n" % i for i in range(90))
        for t in mentions:
            req[1 + 4 * t]["content"] += " and the porcelain teapot"
        return req + [{"role": "user", "content": "and the next one?"}]

    def fold(self, persist, tail_call="", mentions=(), turns=30, tool_lines=90, session="s-persist"):
        """Fold that chat. Returns the level and the weight of the teapot crumb (hot) and of the funnel crumb (cold).
        The last turn calls a tool with tail_call as its command."""
        req = self.chat(turns, mentions, tool_lines)
        if tail_call:
            req[1 + 4 * (turns - 1) + 1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"command": tail_call})
        e = self.engine(target=16_000, cfg={"ace_persist": persist}, session=session)
        self.assertIsNotNone(self.ask(e, req))
        hot, cold = (ace.ref_of(e.store, req[i]["content"]) for i in (7, 11))
        weight = {x["id"]: x["weight"] for x in e._entries(ace.split(req)[1], e.boundary[0], e.boundary[1])}
        return {"hot": e.levels[hot], "cold": e.levels[cold], "w_hot": weight[hot], "w_cold": weight[cold], "layer": e.layer}

    def test_the_words_of_the_tool_calls_of_the_kept_turns_are_focus_words(self):
        """A chat of one group of turns, so that no term comes back in a later group: the words of the call are all."""
        call = "grep porcelain teapot"
        off = self.fold(0, call, turns=7, tool_lines=250)
        on = self.fold(1, call, turns=7, tool_lines=250, session="s-persist-on")
        other = self.fold(1, "uptime", turns=7, tool_lines=250, session="s-persist-other")
        self.assertGreater(on["w_hot"], 2 * off["w_hot"])  # its words are focus words, at half weight
        self.assertGreater(on["w_hot"] / on["w_cold"], 2 * off["w_hot"] / off["w_cold"])
        self.assertLess(other["w_hot"], 1.2 * off["w_hot"])  # a call with other words does not raise the teapot (but 'shelf')

    def test_a_tail_call_word_is_focus_at_half_weight_only_when_a_crumb_has_it_and_it_is_not_focus_already(self):
        e = ace.Ace(target_tokens=16_000)
        e.persist = 1.0
        calls = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "bellows.py", "content": "porcelain teapot, quartz"})}}]}]
        turns = [[{"role": "user", "content": "old"}], [{"role": "user", "content": "new"}] + calls]
        focus = {"quartz": 1.0, "kettle": 0.5}  # focus words of the new message and of the one before
        df = ace.collections.Counter({"porcelain": 1, "teapot": 2, "quartz": 3, "write_file": 1, "path": 4, "bellows": 0})
        e._tail_focus(focus, df, turns, 1, 0)
        self.assertEqual(focus, {"quartz": 1.0, "kettle": 0.5, "porcelain": 0.5, "teapot": 0.5, "write_file": 0.5, "path": 0.5})
        focus = {}  # a turn before the boundary is folded, and is no tail
        e._tail_focus(focus, df, turns, 0, 0)
        self.assertEqual(sorted(focus), ["path", "porcelain", "quartz", "teapot", "write_file"])  # not bellows or content: no crumb has them
        turns = [[{"role": "user", "content": "old"}] + calls, [{"role": "user", "content": "new"}]]
        focus = {}
        e._tail_focus(focus, df, turns, 1, 0)
        self.assertEqual(focus, {})

    def test_terms_that_come_back_in_later_groups_keep_their_crumb(self):
        mentions = (9, 17, 25)  # groups 1, 2 and 3 of 8 turns
        off = self.fold(0, mentions=mentions)
        on = self.fold(1, mentions=mentions, session="s-persist-on")
        self.assertLessEqual(off["hot"], off["cold"])  # off: nothing sets the teapot apart
        self.assertGreater(on["hot"], on["cold"])  # on: a level above the funnel's

    def test_a_term_is_worth_one_step_for_each_later_group_that_uses_it_up_to_four(self):
        for persist in (1.0, 0.5, 2.5):
            weights = {}
            for name, mentions in (("none", ()), ("the same group", (3,)), ("1 group", (9,)), ("3 groups", (9, 17, 25)),
                                   ("4 groups", (9, 17, 25, 33)), ("5 groups", (9, 17, 25, 33, 41))):
                req = self.chat(46, mentions)
                e = self.engine(target=16_000, cfg={"ace_persist": persist}, session="s-%s-%s" % (persist, name.replace(" ", "-")))
                self.ask(e, req)
                hot = ace.ref_of(e.store, req[7]["content"])
                weights[name] = next(x["weight"] for x in e._entries(ace.split(req)[1], e.boundary[0], e.boundary[1]) if x["id"] == hot)
            for name, groups in (("the same group", 0), ("1 group", 1), ("3 groups", 3), ("4 groups", 4), ("5 groups", 4)):
                self.assertAlmostEqual(weights[name] / weights["none"], 1 + persist * groups, places=6, msg=(persist, name))

    def test_persist_at_zero_changes_nothing(self):
        for mentions, call in (((9, 17, 25), "grep porcelain teapot"), ((), "")):
            a = self.fold(0, call, mentions, session="s-a-%d" % len(mentions))
            b = self.fold(0.0, call, mentions, session="s-b-%d" % len(mentions))
            self.assertEqual(a, b)


class DefaultsTest(Kit):
    """With every new dial at its default a request is what it was before the gears."""
    OFF = {"ace_lockup": False, "ace_room": 0, "ace_downshift": 0.0, "ace_turn_fold": False, "ace_persist": 0.0}

    def run_chat(self, cfg, session):
        """The requests that ACE sent for one chat, and the act and the target of each line of its log."""
        e = self.engine(cfg=cfg, session=session)
        msgs, outs = [SYSTEM, ASK], []
        for s in range(260):
            msgs = msgs + step(s, "git commit -qam 'shelf %d'" if s % 40 == 39 else "cat shelf-%d")
            out = self.ask(e, msgs)
            outs.append(json.dumps(out, sort_keys=True))
        mine = [x for x in self.lines() if " sess=%s " % session in x]
        pairs = [(re.search(r" act=(\w+) ", x), re.search(r" target=(\d+) ", x)) for x in mine]
        return outs, [(a.group(1), t.group(1)) for a, t in pairs if a and t]

    def test_dials_set_to_their_defaults_change_nothing(self):
        plain, log_a = self.run_chat(None, "s-plain")
        off, log_b = self.run_chat(self.OFF, "s-off")
        self.assertEqual(plain, off)
        self.assertEqual(log_a, log_b)  # the same act and the same target at every request
        self.assertTrue({"fold", "ratchet"} <= {a for a, _ in log_a}, log_a)  # the chat did fold and raise

    def test_the_first_values_of_the_dogfood_load_from_a_config_file_for_any_model(self):
        """One entry for every model (the key *) holds the five dials: the values that the user sets for the first runs."""
        cfg = {"models": {"*": {"ace_lockup": True, "ace_room": 500_000, "ace_downshift": 0.5, "ace_turn_fold": True,
                                "ace_persist": 0.5}}}
        e = self.engine(cfg=cfg, session="s-dogfood")
        e.update_model("claude-sonnet-5-5", 1_000_000)
        self.assertTrue(e._open())
        self.assertEqual((e.lockup, e.room, e.downshift, e.turn_fold, e.persist), (True, 500_000, 0.5, True, 0.5))
        self.assertEqual(e.gear, "lock")
        other = self.engine(cfg={"models": {"gpt-*": cfg["models"]["*"]}}, session="s-dogfood-2")  # a glob of other models
        other.update_model("claude-sonnet-5-5", 1_000_000)
        self.assertTrue(other._open())
        self.assertEqual((other.lockup, other.room, other.gear), (False, 0, "-"))


if __name__ == "__main__":
    unittest.main()
