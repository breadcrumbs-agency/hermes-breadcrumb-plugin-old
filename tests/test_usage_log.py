"""usage.log: one line of numbers per model request (breadcrumbs/usage_log.py)."""
import os
import re
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from breadcrumbs import usage_log

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_STAMP = usage_log._stamp  # setUp replaces it with a fixed stamp
STAMP = "2026-11-10T15:40:12"
SECRET = "SECRET-TEXT-Y"  # a planted text: it must reach no file
SESS = "20261110_154012_a1b2c3"
KEYS = ["sess", "turn", "req", "n", "task", "model", "resp", "prov", "in", "cr", "cw", "out", "rsn", "prompt", "est",
        "msgs", "tools", "dur", "fin"]
LINE = re.compile(  # the fixed format, field by field
    r"\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d sess=\S+ turn=\S+ req=\S+ n=\d+ task=\S+ model=\S+ resp=\S+ prov=\S+ "
    r"in=\d+ cr=\d+ cw=\d+ out=\d+ rsn=\d+ prompt=\d+ est=\d+ msgs=\d+ tools=\d+ dur=\d+\.\d{3} fin=\S+\n\Z")
API_LINE = (STAMP + " sess=20261110_154012_a1b2c3 turn=turn-7 req=req-42 n=3 task=main model=claude-sonnet-4-6 "
            "resp=claude-sonnet-4-6-20261001 prov=anthropic in=120 cr=18000 cw=500 out=250 rsn=40 prompt=18620 est=0 "
            "msgs=41 tools=0 dur=3.412 fin=tool_calls\n")
AUX_LINE = (STAMP + " sess=20261110_154012_a1b2c3 turn=turn-7 req=aux-0123abcd n=1 task=title_generation "
            "model=claude-haiku-4-5 resp=claude-haiku-4-5 prov=anthropic in=256 cr=0 cw=0 out=12 rsn=0 prompt=256 "
            "est=0 msgs=2 tools=0 dur=0.420 fin=stop\n")
USAGE = {"input_tokens": 120, "output_tokens": 250, "cache_read_tokens": 18000, "cache_write_tokens": 500,
         "reasoning_tokens": 40, "request_count": 1, "prompt_tokens": 18620, "total_tokens": 18870}
API_READS = {"usage", "session_id", "turn_id", "api_request_id", "api_call_count", "model", "response_model",
             "provider", "approx_input_tokens", "message_count", "tool_count", "api_duration", "finish_reason"}
AUX_READS = API_READS | {"aux_task", "error", "error_type"}  # error and error_type: said "failed", never printed
USAGE_READS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
               "prompt_tokens"}
TEXTS = {"request", "response", "base_url", "assistant_message"}  # what a line never reads

# The payloads that `hermes hooks test` sends: hermes_cli/hooks.py, _DEFAULT_PAYLOADS (copied).
HERMES_API = {
    "session_id": "test-session", "task_id": "test-task", "platform": "cli",
    "model": "claude-sonnet-4-6", "provider": "anthropic",
    "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages",
    "api_call_count": 1, "api_duration": 1.234,
    "started_at": 1756000000.0, "ended_at": 1756000001.234, "first_chunk_at": 1756000000.512,
    "finish_reason": "stop", "message_count": 4, "response_model": "claude-sonnet-4-6",
    "usage": {"input_tokens": 2048, "output_tokens": 512}, "context_length": 200000,
    "assistant_content_chars": 1200, "assistant_tool_call_count": 0, "moa_references": None,
}
HERMES_AUX = {
    "aux_task": "title_generation", "session_id": "test-session", "task_id": "test-task",
    "turn_id": "test-turn", "api_request_id": "aux-0123abcd", "platform": "cli",
    "model": "claude-haiku-4-5", "provider": "anthropic",
    "base_url": "https://api.anthropic.com", "api_mode": "anthropic_messages",
    "api_call_count": 1, "retry_count": 0, "streaming": False, "api_duration": 0.42,
    "started_at": 1756000000.0, "ended_at": 1756000000.42, "finish_reason": "stop",
    "response_model": "claude-haiku-4-5", "usage": {"input_tokens": 256, "output_tokens": 12},
    "assistant_content_chars": 40, "assistant_tool_call_count": 0, "error": None, "error_type": None,
}


def api_kw(**over):
    """What Hermes passes to post_api_request (agent/turn_response_intake.py), with the text that it carries."""
    kw = dict(
        task_id="task-1", turn_id="turn-7", api_request_id="req-42", session_id=SESS, platform="cli",
        model="claude-sonnet-4-6", provider="anthropic", base_url="https://api.example.invalid/v1",
        api_mode="anthropic_messages", api_call_count=3, api_duration=3.4116, started_at=1.0, ended_at=4.4116,
        first_chunk_at=1.5, finish_reason="tool_calls", message_count=41, response_model="claude-sonnet-4-6-20261001",
        response={"assistant_message": {"role": "assistant", "content": "reply text"}}, usage=dict(USAGE),
        context_length=200000, assistant_message="reply text", assistant_content_chars=10,
        assistant_tool_call_count=2, moa_references=None)
    kw.update(over)
    return kw


def aux_kw(**over):
    """What Hermes passes to post_auxiliary_call (agent/auxiliary_hooks.py), with the text that it carries."""
    kw = dict(
        aux_task="title_generation", session_id=SESS, task_id="task-1", turn_id="turn-7", platform="cli",
        api_request_id="aux-0123abcd", retry_count=0, api_call_count=1, model="claude-haiku-4-5",
        provider="anthropic", base_url="https://api.example.invalid/v1", api_mode="anthropic_messages",
        streaming=False, started_at=1.0, message_count=2, ended_at=1.42, api_duration=0.42, error=None,
        error_type=None, finish_reason="stop", response_model="claude-haiku-4-5",
        usage={"input_tokens": 256, "output_tokens": 12, "cache_read_tokens": 0, "cache_write_tokens": 0,
               "reasoning_tokens": 0, "request_count": 1, "prompt_tokens": 256, "total_tokens": 268},
        response={"assistant_message": {"role": "assistant", "content": "title text"}},
        assistant_content_chars=10, assistant_tool_call_count=0)
    kw.update(over)
    return kw


def fields(text):
    """The fields of a line as a dict of strings (a value never holds a space)."""
    return dict(f.split("=", 1) for f in text.split()[1:])


class Ctx:
    """A Hermes with plugin hooks: it keeps each callback by name, in the order of registration."""

    def __init__(self):
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback


class Reads(dict):
    """A payload that keeps the names of the fields that were read ("*": all of them at once)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __contains__(self, key):
        self.read.add(key)
        return super().__contains__(key)

    def __iter__(self):
        self.read.add("*")
        return super().__iter__()

    def __repr__(self):
        self.read.add("*")
        return super().__repr__()

    def keys(self):
        self.read.add("*")
        return super().keys()

    def values(self):
        self.read.add("*")
        return super().values()

    def items(self):
        self.read.add("*")
        return super().items()


class UsageLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name  # the profile home: every test writes here and nowhere else
        env = mock.patch.dict(os.environ, {"HERMES_HOME": self.home})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(usage_log.OFF, None)
        stamp = mock.patch.object(usage_log, "_stamp", return_value=STAMP)
        stamp.start()
        self.addCleanup(stamp.stop)
        self.path = os.path.join(self.home, "breadcrumbs", "usage.log")
        self.assertEqual(usage_log.log_path(), self.path)  # a guard: a test never reaches ~/.hermes
        self.assertNotEqual(self.path, os.path.expanduser("~/.hermes/breadcrumbs/usage.log"))
        ctx = Ctx()
        usage_log.register(ctx)
        self.api, self.aux = ctx.hooks["post_api_request"], ctx.hooks["post_auxiliary_call"]

    def lines(self, name="usage.log"):
        with open(os.path.join(self.home, "breadcrumbs", name), encoding="utf-8", newline="") as f:
            return f.read().splitlines(keepends=True)

    def files(self):
        return [os.path.join(top, n) for top, _, names in os.walk(self.home) for n in names]

    # -- the line ---------------------------------------------------------------------------------------------
    def test_a_request_writes_the_exact_line(self):
        self.assertIsNone(self.api(**api_kw()))
        self.assertEqual(self.lines(), [API_LINE])
        self.assertEqual(os.listdir(os.path.join(self.home, "breadcrumbs")), ["usage.log"])

    def test_an_aux_call_writes_its_task_in_the_task_field(self):
        self.assertIsNone(self.aux(**aux_kw()))
        self.aux(**aux_kw(aux_task="context_compression", api_request_id="aux-9", api_call_count=2))
        first, second = self.lines()
        self.assertEqual(first, AUX_LINE)
        self.assertIn(" task=context_compression ", second)
        self.assertIn(" req=aux-9 n=2 ", second)
        self.assertNotIn("task=main", first + second)

    def test_the_line_is_the_fixed_format(self):
        self.api(**api_kw())
        self.aux(**aux_kw())
        for text in self.lines():
            self.assertRegex(text, LINE)
            self.assertEqual(text[19], " ")  # the stamp is 19 characters, then one space
            self.assertEqual([f.split("=")[0] for f in text.split()[1:]], KEYS)

    def test_hermes_own_test_payloads_write_their_lines(self):
        self.api(**HERMES_API)
        self.aux(**HERMES_AUX)
        self.assertEqual(self.lines(), [
            STAMP + " sess=test-session turn=- req=- n=1 task=main model=claude-sonnet-4-6 resp=claude-sonnet-4-6 "
                    "prov=anthropic in=2048 cr=0 cw=0 out=512 rsn=0 prompt=2048 est=0 msgs=4 tools=0 dur=1.234 "
                    "fin=stop\n",
            STAMP + " sess=test-session turn=test-turn req=aux-0123abcd n=1 task=title_generation "
                    "model=claude-haiku-4-5 resp=claude-haiku-4-5 prov=anthropic in=256 cr=0 cw=0 out=12 rsn=0 "
                    "prompt=256 est=0 msgs=0 tools=0 dur=0.420 fin=stop\n"])

    def test_the_stamp_is_local_time_in_the_form_of_ace_log(self):
        stamp = REAL_STAMP()
        self.assertRegex(stamp, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertLess(abs(time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S")) - time.time()), 5)  # not UTC
        with mock.patch.object(usage_log.time, "strftime", return_value="X") as strftime:
            self.assertEqual(REAL_STAMP(), "X")
        strftime.assert_called_once_with("%Y-%m-%dT%H:%M:%S")  # as Ace._log writes it

    def test_est_and_tools_are_written_when_hermes_passes_them(self):
        # Hermes sends them to the pre_* hooks only; a Hermes that adds them to the post_* hooks gets them here.
        self.api(**api_kw(approx_input_tokens=18700, tool_count=12))
        [text] = self.lines()
        self.assertIn(" est=18700 msgs=41 tools=12 ", text)

    # -- usage ------------------------------------------------------------------------------------------------
    def test_a_call_with_no_usage_writes_zeros(self):
        self.api(**api_kw(usage=None))
        self.aux(**aux_kw(usage=None, finish_reason=None, response_model=None))
        first, second = self.lines()
        self.assertIn(" in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 ", first)
        self.assertTrue(first.endswith(" fin=tool_calls\n"))
        self.assertIn(" resp=- prov=anthropic in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 ", second)
        self.assertTrue(second.endswith(" fin=-\n"))

    def test_a_failed_aux_call_writes_its_line_with_zeros_and_fin_error(self):
        self.aux(**aux_kw(usage=None, finish_reason=None, response_model=None, response=None, api_duration=30.0004,
                          error="TimeoutError: " + SECRET, error_type="TimeoutError"))
        self.assertEqual(self.lines(), [
            STAMP + " sess=20261110_154012_a1b2c3 turn=turn-7 req=aux-0123abcd n=1 task=title_generation "
                    "model=claude-haiku-4-5 resp=- prov=anthropic in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 est=0 msgs=2 "
                    "tools=0 dur=30.000 fin=error\n"])
        with open(self.path, "rb") as f:
            self.assertNotIn(SECRET.encode(), f.read())

    def test_nothing_given_writes_dashes_and_zeros(self):
        self.api()
        self.aux()
        self.assertEqual(self.lines(), [
            STAMP + " sess=- turn=- req=- n=0 task=main model=- resp=- prov=- in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 "
                    "est=0 msgs=0 tools=0 dur=0.000 fin=-\n",
            STAMP + " sess=- turn=- req=- n=0 task=- model=- resp=- prov=- in=0 cr=0 cw=0 out=0 rsn=0 prompt=0 "
                    "est=0 msgs=0 tools=0 dur=0.000 fin=-\n"])

    def test_prompt_is_hermes_count_else_the_sum_of_input_and_cache(self):
        self.api(**api_kw(usage={"input_tokens": 10, "cache_read_tokens": 200, "cache_write_tokens": 3000,
                                 "output_tokens": 4}))
        self.api(**api_kw(usage=dict(USAGE, prompt_tokens=3333)))
        first, second = self.lines()
        self.assertIn(" in=10 cr=200 cw=3000 out=4 rsn=0 prompt=3210 ", first)
        self.assertIn(" prompt=3333 ", second)

    def test_a_usage_object_with_attributes_reads_as_a_dict_does(self):
        usage = types.SimpleNamespace(input_tokens=5, output_tokens=6, cache_read_tokens=7, cache_write_tokens=8,
                                      reasoning_tokens=9, prompt_tokens=20)
        self.api(**api_kw(usage=usage))
        [text] = self.lines()
        self.assertIn(" in=5 cr=7 cw=8 out=6 rsn=9 prompt=20 ", text)

    def test_a_number_is_whole_and_never_below_zero(self):
        bad = {"input_tokens": "15", "output_tokens": -3, "cache_read_tokens": None, "cache_write_tokens": True,
               "reasoning_tokens": float("nan"), "prompt_tokens": 12.9}
        self.api(**api_kw(api_call_count="7", message_count=12.9, tool_count=float("inf"), approx_input_tokens=-5,
                          usage=bad, api_duration=float("nan")))
        self.api(**api_kw(api_duration=-2.5))
        self.api(**api_kw(api_duration=2))
        self.api(**api_kw(api_duration=1.23456))
        self.api(**api_kw(api_duration="slow"))
        first, *rest = self.lines()
        self.assertIn(" n=7 ", first)
        self.assertIn(" in=15 cr=0 cw=0 out=0 rsn=0 prompt=12 est=0 msgs=12 tools=0 dur=0.000 ", first)
        self.assertEqual([t.split(" dur=")[1].split()[0] for t in rest], ["0.000", "2.000", "1.235", "0.000"])
        for text in self.lines():
            self.assertRegex(text, LINE)

    # -- values -----------------------------------------------------------------------------------------------
    def test_a_value_never_holds_a_space(self):
        self.api(**api_kw(model="my model\tv2\n", session_id="  a b  ", finish_reason="end turn",
                          provider="x\u00a0y", turn_id="t\r\n1", api_request_id="a\x07b"))
        [text] = self.lines()  # one line, whatever the values held
        got = fields(text)
        self.assertEqual((got["model"], got["sess"], got["fin"], got["prov"], got["turn"], got["req"]),
                         ("my_model_v2", "a_b", "end_turn", "x_y", "t_1", "a_b"))
        self.assertRegex(text, LINE)

    def test_a_structure_is_no_id_and_a_long_value_is_cut(self):
        self.api(**api_kw(turn_id={"a": "b c"}, api_request_id=["x"], model="m" * 1000, session_id=b"bytes"))
        [text] = self.lines()
        got = fields(text)
        self.assertEqual((got["turn"], got["req"], got["sess"]), ("-", "-", "-"))
        self.assertEqual(got["model"], "m" * usage_log.WORD_MAX)

    # -- no text ----------------------------------------------------------------------------------------------
    def test_a_planted_text_reaches_no_file(self):
        planted = dict(
            request={"method": "POST", "body": {"messages": [{"role": "user", "content": SECRET}]}},
            response={"assistant_message": {"content": SECRET, "tool_calls": [{"function": {"arguments": SECRET}}]}},
            assistant_message=SECRET, base_url="https://%s.example.invalid" % SECRET, system_prompt=SECRET,
            request_messages=[SECRET], note=SECRET)
        self.api(**api_kw(**planted))
        self.aux(**aux_kw(**planted))
        self.aux(**aux_kw(**dict(planted, usage=None, finish_reason=None, response_model=None,
                                 error="RuntimeError: " + SECRET, error_type="RuntimeError")))
        self.api(**api_kw(**dict(planted, usage=dict(USAGE, raw_usage={"text": SECRET}, text=SECRET))))
        names = sorted(os.path.relpath(p, self.home) for p in self.files())
        self.assertEqual(names, [os.path.join("breadcrumbs", "usage.log")])  # not vacuous: the lines are there
        self.assertEqual(len(self.lines()), 4)
        for path in self.files():
            with open(path, "rb") as f:
                self.assertNotIn(SECRET.encode(), f.read(), path)

    def test_a_line_reads_the_named_fields_and_nothing_else(self):
        kw = Reads(api_kw(error=SECRET, request={"x": SECRET}), usage=Reads(USAGE))
        usage_log.api_line(kw)
        self.assertEqual(kw.read, API_READS)
        self.assertEqual(kw["usage"].read, USAGE_READS)
        for failed in (None, SECRET):
            kw = Reads(aux_kw(request={"x": SECRET}, error=failed, error_type="E" if failed else None), usage=Reads())
            usage_log.aux_line(kw)
            self.assertLessEqual(kw.read, AUX_READS)
            self.assertFalse(kw.read & TEXTS, kw.read)
            self.assertNotIn("*", kw.read)

    def test_a_failure_logs_the_kind_of_error_and_never_its_text(self):
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError(SECRET)

        with self.assertLogs("breadcrumbs.usage_log", level="DEBUG") as logs:
            self.assertIsNone(self.api(**api_kw(usage=Boom())))
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Traceback", text)
        self.assertFalse(os.path.exists(self.path))

    # -- the file ---------------------------------------------------------------------------------------------
    def test_the_file_turns_over_to_usage_log_1_at_log_bytes(self):
        one = len(API_LINE.encode())  # every line holds the same bytes: n is a single digit
        ns = lambda name: [int(fields(t)["n"]) for t in self.lines(name)]  # noqa: E731
        with mock.patch.object(usage_log, "LOG_BYTES", 2 * one):  # the file may grow to 2 lines, then 1 more
            for n in (1, 2, 3):
                self.api(**api_kw(api_call_count=n))
            self.assertFalse(os.path.exists(self.path + ".1"))  # at LOG_BYTES, not over it: no turn-over yet
            self.assertEqual(ns("usage.log"), [1, 2, 3])
            for n in (4, 5):
                self.api(**api_kw(api_call_count=n))
            self.assertEqual((ns("usage.log.1"), ns("usage.log")), ([1, 2, 3], [4, 5]))
            for n in (6, 7):
                self.api(**api_kw(api_call_count=n))
            self.assertEqual((ns("usage.log.1"), ns("usage.log")), ([4, 5, 6], [7]))  # one old file, as ace.log
        for name in ("usage.log.1", "usage.log"):
            for text in self.lines(name):
                self.assertRegex(text, LINE)  # a line is never split between the two files

    def test_log_bytes_is_one_mebibyte_as_in_ace_log(self):
        from breadcrumbs import ace
        self.assertEqual(usage_log.LOG_BYTES, 1 << 20)
        self.assertEqual(usage_log.LOG_BYTES, ace.LOG_BYTES)

    def test_a_line_is_one_write_call_under_the_lock(self):
        writes = []

        class File:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                writes.append((data, usage_log._LOCK.locked()))
                return len(data)

        with mock.patch.object(usage_log, "open", create=True, side_effect=lambda *a, **k: File()):
            self.api(**api_kw())
        self.assertEqual(writes, [(API_LINE.encode(), True)])

    def test_threads_append_whole_lines(self):
        def work(who):
            for n in range(40):
                self.api(**api_kw(session_id="t%d" % who, api_call_count=n))

        threads = [threading.Thread(target=work, args=(who,)) for who in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = self.lines()
        self.assertEqual(len(lines), 8 * 40)
        by_thread = {}
        for text in lines:
            self.assertRegex(text, LINE)
            got = fields(text)
            by_thread.setdefault(got["sess"][1:], []).append(int(got["n"]))
        self.assertEqual(by_thread, {str(who): list(range(40)) for who in range(8)})

    # -- the switch and the home ------------------------------------------------------------------------------
    def test_the_off_switch_writes_nothing(self):
        with mock.patch.dict(os.environ, {usage_log.OFF: "0"}):
            self.api(**api_kw())
            self.aux(**aux_kw())
            self.aux(**aux_kw(usage=None, error=SECRET, error_type="E"))
        self.assertEqual(os.listdir(self.home), [])  # no line, no file, not even the folder
        with mock.patch.dict(os.environ, {usage_log.OFF: "1"}):
            self.api(**api_kw())
        self.api(**api_kw())  # the switch is read at each call: back to unset, it writes
        self.assertEqual(self.lines(), [API_LINE, API_LINE])

    def test_the_profile_home_comes_from_hermes_home(self):
        with tempfile.TemporaryDirectory() as other:
            # another profile: the home is read at each call, not at register
            with mock.patch.dict(os.environ, {"HERMES_HOME": other}):
                self.assertEqual(usage_log.log_path(), os.path.join(other, "breadcrumbs", "usage.log"))
                self.api(**api_kw(session_id="in-other"))
            self.api(**api_kw(session_id="in-first"))
            with open(os.path.join(other, "breadcrumbs", "usage.log"), encoding="utf-8") as f:
                other_text = f.read()
        self.assertIn("sess=in-other ", other_text)
        self.assertNotIn("in-first", other_text)
        [mine] = self.lines()
        self.assertIn("sess=in-first ", mine)
        self.assertNotIn("in-other", mine)

    def test_the_hooks_never_raise(self):
        with mock.patch.object(usage_log, "record", side_effect=RuntimeError(SECRET)):
            self.assertIsNone(self.api(**api_kw()))
            self.assertIsNone(self.aux(**aux_kw()))
        blocker = os.path.join(self.home, "blocker")
        with open(blocker, "w"):
            pass
        with mock.patch.dict(os.environ, {"HERMES_HOME": blocker}):  # a file where the folder should be
            self.assertIsNone(self.api(**api_kw()))
            self.assertIsNone(self.aux(**aux_kw()))
        with mock.patch.object(usage_log, "log_path", side_effect=ImportError("no home")):
            self.assertIsNone(self.api(**api_kw()))
        self.assertEqual(sorted(os.listdir(self.home)), ["blocker"])

    # -- the registration -------------------------------------------------------------------------------------
    def test_register_gives_the_two_observer_hooks_where_hermes_has_hooks(self):
        ctx = Ctx()
        self.assertEqual(usage_log.register(ctx), ["post_api_request", "post_auxiliary_call"])
        self.assertEqual(list(ctx.hooks), ["post_api_request", "post_auxiliary_call"])
        self.assertEqual(usage_log.HOOKS, ("post_api_request", "post_auxiliary_call"))
        self.assertEqual(os.listdir(self.home), [])  # registering writes nothing

        class Refuses:
            def register_hook(self, name, callback):
                raise ValueError(name)

        self.assertEqual(usage_log.register(object()), [])  # a Hermes with no hooks gets nothing
        self.assertEqual(usage_log.register(Refuses()), [])  # a Hermes that refuses a hook still loads the plugin

    def test_the_manifest_declares_both_hooks(self):
        with open(os.path.join(ROOT, "plugin.yaml"), encoding="utf-8") as f:
            [declared] = [line.partition(":")[2] for line in f if line.startswith("provides_hooks:")]
        self.assertLessEqual(set(usage_log.HOOKS), {h.strip() for h in declared.strip().strip("[]").split(",")})

    def test_the_root_registration_reaches_the_file_right_after_the_islands(self):
        from breadcrumbs import hermes
        ctx = Ctx()
        hermes.register_plugin(ctx, mock.Mock())  # the merged tree has no rebind of Hermes core to keep out (intercept, dials.install)
        self.assertEqual(list(ctx.hooks)[-6:-1], ["on_session_start", "post_tool_call", "transform_llm_output",
                                                  "post_api_request", "post_auxiliary_call"])  # then the hook of the family
        ctx.hooks["post_api_request"](**api_kw())
        ctx.hooks["post_auxiliary_call"](**aux_kw())
        self.assertEqual(self.lines(), [API_LINE, AUX_LINE])


if __name__ == "__main__":
    unittest.main()
