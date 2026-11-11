import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock

from breadcrumbs.crumbs import get_crumb
from breadcrumbs.history import Refs
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.index import Index
from breadcrumbs.ingest import put_tree, put_turn
from breadcrumbs.lines import get_text
from breadcrumbs.store import Store
from breadcrumbs import cli
from breadcrumbs import summarize as summarize_mod
from breadcrumbs.summarize import Llama, PromptTooLong, clean, default_runner, source_text, summarize
from breadcrumbs.zoom import Zoom

OUT = "The user set the nightly job to run at two. The assistant moved the logs to the build host."


class Fake:
    """A stand-in model: records each prompt, returns OUT."""
    name = "fake"
    params = {"n": 120, "ctx": 4096, "temp": 0}

    def __init__(self, out=OUT):
        self.out, self.calls = out, []

    def ident(self):
        return {"model": "fake.gguf", "model_sha256": "ab" * 32}

    def prompt(self, text):
        return "SUMMARIZE:\n" + text

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.index = Index(":memory:")

    def tearDown(self):
        self.index.close()
        self.store.close()
        self.tmp.cleanup()

    def session(self, n, size=200, sid="s1"):
        turns, prev = [], None
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            text = ("turn %d about the nightly job and the logs. " % i * 20)[:size]
            prev = put_turn(self.store, self.index, role, text, sid, ts=1000 + i, prev=prev)
            turns.append(prev)
        return put_tree(self.store, "sess", "session", turns, meta={"sess": sid}), turns


class SummaryCrumbTest(Base):
    def test_summary_records_source_model_and_exact_input(self):
        sess, _ = self.session(20)
        fake = Fake()
        sid = summarize(self.store, self.index, sess, fake)
        c = get_crumb(self.store, sid)
        self.assertEqual(c.kind, "sum")
        self.assertEqual(c.heritage, [sess])
        self.assertEqual(c.meta["model_sha256"], "ab" * 32)
        self.assertEqual(c.meta["sess"], "s1")
        self.assertEqual(get_text(self.store, c.meta["input"]).decode("utf-8"), fake.calls[0])
        self.assertIn(OUT, Zoom(self.store).render(sid, 1.0))
        self.assertEqual(self.index.summary_of(sess), sid)

    def test_summary_is_searchable(self):
        sess, _ = self.session(20)
        sid = summarize(self.store, self.index, sess, Fake())
        self.assertIn(sid, [c for c, _r, _s in self.index.search("build host", 5)])

    def test_a_second_call_reuses_the_summary(self):
        sess, _ = self.session(20)
        fake = Fake()
        first = summarize(self.store, self.index, sess, fake)
        self.assertEqual(summarize(self.store, self.index, sess, fake), first)
        self.assertEqual(len(fake.calls), 1)

    def test_turns_and_small_crumbs_get_no_summary(self):
        sess, turns = self.session(2, size=40)
        fake = Fake()
        self.assertIsNone(summarize(self.store, self.index, turns[0], fake))
        self.assertIsNone(summarize(self.store, self.index, sess, fake))
        self.assertEqual(fake.calls, [])

    def test_a_label_copied_from_the_prompt_is_dropped(self):
        sess, _ = self.session(20)
        sid = summarize(self.store, self.index, sess, Fake(out="LOG:  \n- " + OUT))
        self.assertEqual(Zoom(self.store).render(sid, 1.0).splitlines()[1], "- " + OUT)
        self.assertEqual(clean("Summary: two facts."), "two facts.")
        self.assertEqual(clean("The LOG: line stays."), "The LOG: line stays.")

    def test_an_empty_model_answer_makes_no_crumb(self):
        sess, _ = self.session(20)
        self.assertIsNone(summarize(self.store, self.index, sess, Fake(out="  \n")))
        self.assertIsNone(self.index.summary_of(sess))


class TooLong(Fake):
    """A model with a small context: about 2 bytes per token, like code and ids."""

    def __init__(self, limit_tokens):
        super().__init__()
        self.limit = limit_tokens

    def __call__(self, prompt):
        tokens = len(prompt.encode("utf-8")) // 2
        if tokens > self.limit:
            self.calls.append(None)
            raise PromptTooLong(tokens, self.limit)
        return super().__call__(prompt)


class RetryTest(Base):
    def test_a_too_long_prompt_is_retried_with_less_text(self):
        sess, _ = self.session(40, size=300)  # about 12 KB of source text
        model = TooLong(limit_tokens=2500)  # fits about 5,000 bytes
        sid = summarize(self.store, self.index, sess, model)
        self.assertTrue(sid)
        self.assertGreaterEqual(len(model.calls), 2)
        c = get_crumb(self.store, sid)
        self.assertEqual(get_text(self.store, c.meta["input"]).decode("utf-8"), model.calls[-1])
        self.assertLessEqual(len(model.calls[-1].encode("utf-8")) // 2, 2500)

    def test_a_model_that_never_fits_gives_no_summary(self):
        sess, _ = self.session(40, size=300)
        self.assertIsNone(summarize(self.store, self.index, sess, TooLong(limit_tokens=5)))
        self.assertIsNone(self.index.summary_of(sess))


class ZoomSummaryTest(Base):
    def setUp(self):
        super().setUp()
        self.sess, self.turns = self.session(20)
        self.sid = summarize(self.store, self.index, self.sess, Fake())
        self.zoom = Zoom(self.store, summaries=self.index.summary_of)

    def test_a_low_level_shows_the_summary_first(self):
        lines = self.zoom.render(self.sess, 0.1).splitlines()
        self.assertTrue(lines[1].strip().startswith("\u25b8 %s sum" % self.sid), lines[:3])
        self.assertEqual(lines[2], OUT)

    def test_every_summary_line_comes_before_the_parts(self):
        sess, _ = self.session(20, sid="s2")
        two = "First line of the summary.\nSecond line of the summary.\n"
        summarize(self.store, self.index, sess, Fake(out=two))
        lines = self.zoom.render(sess, 0.1).splitlines()
        self.assertEqual(lines[2:4], two.splitlines(), lines[:5])

    def test_level_one_keeps_every_source_line(self):
        full = self.zoom.render(self.sess, 1.0)
        plain = Zoom(self.store).render(self.sess, 1.0)
        for line in plain.splitlines()[1:]:
            self.assertIn(line, full)
        self.assertIn(OUT, full)

    def test_a_higher_level_never_drops_a_line(self):
        prev = set()
        for i in range(1, 21):
            got = set(self.zoom.render(self.sess, i / 20).splitlines())
            kept = {l for l in prev if not l.lstrip().startswith(("\u22ef", "\u25b8"))}
            self.assertLessEqual(kept, got, i)
            prev = got

    def test_a_summary_has_no_summary(self):
        self.index.set_summary(self.sid, self.sid)  # even a bad index row makes no loop
        self.assertEqual(self.zoom.render(self.sid, 1.0).count(OUT), 1)


class InputTest(Base):
    def test_short_turns_stay_whole_and_long_turns_are_cut(self):
        turns, prev = [], None
        for i in range(10):
            prev = put_turn(self.store, self.index, "user", "short turn number %d." % i, "s1", ts=i, prev=prev)
            turns.append(prev)
        turns.append(put_turn(self.store, self.index, "assistant", "word " * 4000, "s1", ts=11, prev=prev))
        sess = put_tree(self.store, "sess", "session", turns, meta={"sess": "s1"})
        text = source_text(self.store, sess, max_input=3000)
        self.assertLessEqual(len(text.encode("utf-8")), 3000)
        for i in range(10):
            self.assertIn("user: short turn number %d.\n" % i, text)
        self.assertIn("assistant: word word", text)
        self.assertTrue(text.rstrip("\n").endswith("\u2026"))

    def test_a_session_uses_the_summaries_of_its_spans(self):
        sess, turns = self.session(40)  # spans of 16, 16 and 8 turns
        spans = get_crumb(self.store, sess).kids
        for span in spans[:2]:
            self.assertTrue(summarize(self.store, self.index, span, Fake(out="SPAN SUMMARY.")))
        text = source_text(self.store, sess, summaries=self.index.summary_of)
        self.assertEqual(text.count("summary: SPAN SUMMARY."), 2)
        self.assertNotIn("turn 0 about", text)
        for i in range(32, 40):
            self.assertIn("turn %d about" % i, text)


class ProviderSummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = BreadcrumbsProvider()
        self.p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.fake = self.p.summarizer = Fake()

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def turns(self, n):
        for i in range(n):
            self.p.sync_turn("question %d about the nightly job and the logs. " % i * 8,
                             "answer %d: the job moved to the build host. " % i * 8, session_id="s1")

    def test_closed_spans_get_summaries_in_the_background(self):
        self.turns(17)  # 34 turn crumbs: spans of 16, 16 and 2
        self.p.flush_summaries()
        spans = get_crumb(self.p.store, self.p.session_crumb("s1")).kids
        self.assertTrue(self.p.index.summary_of(spans[0]))
        self.assertTrue(self.p.index.summary_of(spans[1]))
        self.assertIsNone(self.p.index.summary_of(spans[2]))
        self.assertEqual(len(self.fake.calls), 2)

    def test_session_end_summarizes_the_session(self):
        self.turns(4)
        self.p.on_session_end([])
        self.p.flush_summaries()
        sess = self.p.session_crumb("s1")
        self.assertTrue(self.p.index.summary_of(sess))
        text = json.loads(self.p.handle_tool_call("crumb", {"id": sess, "level": 0.3}))["text"]
        self.assertIn(OUT, text)

    def test_the_worker_retries_a_too_long_prompt(self):
        self.fake = self.p.summarizer = TooLong(limit_tokens=1500)  # fits about 3,000 bytes
        self.turns(17)
        self.p.flush_summaries()
        spans = get_crumb(self.p.store, self.p.session_crumb("s1")).kids
        self.assertTrue(self.p.index.summary_of(spans[0]))
        self.assertIn(None, self.fake.calls)  # at least one call was too long

    def test_the_worker_starts_through_hermes_spawn_context_thread(self):
        # Hermes rule: provider threads carry the profile's contextvars (spawn_context_thread).
        names = []

        def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
            names.append(name)
            return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs)
        mp = types.ModuleType("agent.memory_provider")
        vars(mp)["spawn_context_thread"] = spawn_context_thread
        agent = types.ModuleType("agent")
        vars(agent)["memory_provider"] = mp
        with mock.patch.dict(sys.modules, {"agent": agent, "agent.memory_provider": mp}):
            self.turns(17)
            self.p.flush_summaries()
        self.assertEqual(names, ["breadcrumbs-summaries"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_no_summarizer_means_no_model_calls(self):
        self.p.summarizer = None
        self.turns(17)
        self.p.on_session_end([])
        self.p.flush_summaries()
        self.assertEqual(self.fake.calls, [])


class RunnerTest(Base):
    def test_summarize_0_turns_the_model_off(self):
        old = os.environ.get("BREADCRUMBS_SUMMARIZE")
        os.environ["BREADCRUMBS_SUMMARIZE"] = "0"
        try:
            self.assertIsNone(default_runner())
        finally:
            if old is None:
                del os.environ["BREADCRUMBS_SUMMARIZE"]
            else:
                os.environ["BREADCRUMBS_SUMMARIZE"] = old

    def test_a_missing_model_is_not_available(self):
        self.assertFalse(Llama(model=os.path.join(self.tmp.name, "none.gguf")).available())

    def test_the_prompt_is_chatml_with_thinking_off(self):
        p = Llama(model="x.gguf").prompt("user: hello\n")
        self.assertTrue(p.startswith("<|im_start|>system\n"))
        self.assertIn("user: hello<|im_end|>", p)  # content is trimmed, as the official templates do
        self.assertTrue(p.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"))

    def test_cli_keeps_going_after_a_failed_summary(self):
        p = BreadcrumbsProvider()
        p.initialize("s8", hermes_home=self.tmp.name + "/g", platform="cli", agent_context="primary")
        p.summarizer = None
        for sid in ("s8", "s9"):
            for i in range(4):
                p.sync_turn("question %d about the nightly job and the logs. " % i * 8, "answer %d. " % i * 30, session_id=sid)
        p.shutdown()

        class FailOnce(Fake):
            def __call__(self, prompt):
                if not self.calls:
                    self.calls.append(prompt)
                    raise RuntimeError("model crashed")
                return super().__call__(prompt)

        old = summarize_mod.Llama
        summarize_mod.Llama = lambda: FailOnce()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(["--home", self.tmp.name + "/g/breadcrumbs", "summarize", "--all"])
        finally:
            summarize_mod.Llama = old
        res = json.loads(out.getvalue())
        self.assertEqual((code, res["made"], res["failed"]), (1, 1, 1))
        refs = Refs(os.path.join(self.tmp.name, "g", "breadcrumbs", "refs"))
        self.assertTrue(refs.get("summaries"))  # what was made is committed

    def test_cli_summarizes_every_session(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        p = BreadcrumbsProvider()
        p.initialize("s7", hermes_home=self.tmp.name + "/h", platform="cli", agent_context="primary")
        p.summarizer = None
        for i in range(4):
            p.sync_turn("question %d about the nightly job and the logs. " % i * 8, "answer %d. " % i * 30, session_id="s7")
        sess = p.session_crumb("s7")
        p.shutdown()
        old = summarize_mod.Llama
        summarize_mod.Llama = lambda: Fake()
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--home", self.tmp.name + "/h/breadcrumbs", "summarize", "--all"]), 0)
            self.assertEqual(json.loads(out.getvalue())["made"], 1)
        finally:
            summarize_mod.Llama = old
        index = Index(os.path.join(self.tmp.name, "h", "breadcrumbs", "index.db"))
        self.assertTrue(index.summary_of(sess))
        index.close()


class TemplateTest(unittest.TestCase):
    def test_the_default_model_is_qwen3_5_0_8b(self):
        # Chosen on the best answer and route@1 scores of ten local models up to 1.5B (Apache-2.0)
        # and the full LoCoMo run.
        self.assertEqual(os.path.basename(summarize_mod.MODEL), "Qwen_Qwen3.5-0.8B-Q4_K_M.gguf")
        self.assertEqual(Llama(model="/x.gguf").params["template"], "qwen")

    def test_the_default_template_is_qwen_with_thinking_off(self):
        p = Llama(model="/x.gguf").prompt("LINE ONE")
        self.assertTrue(p.startswith("<|im_start|>system\n" + summarize_mod.SYSTEM + "<|im_end|>\n"))
        self.assertIn("LINE ONE<|im_end|>\n", p)
        self.assertTrue(p.endswith("<|im_start|>assistant\n<think>\n\n</think>\n\n"))

    def test_each_family_wraps_the_same_text(self):
        ends = {"chatml": "<|im_start|>assistant\n", "nemotron": "<|im_start|>assistant\n<think></think>",
                "gemma3": "<start_of_turn>model\n", "gemma4": "<|turn>model\n",
                "granite": "<|start_of_role|>assistant<|end_of_role|>"}
        for name, end in ends.items():
            p = Llama(model="/x.gguf", template=name).prompt("LINE ONE")
            self.assertTrue(p.endswith(end), name)
            self.assertIn(summarize_mod.SYSTEM, p)
            self.assertIn(summarize_mod.ASK.strip() + "\nLINE ONE", p)
        self.assertEqual(Llama(model="/x.gguf", template="granite").params["template"], "granite")

    def test_the_environment_picks_a_template_and_a_bad_name_fails(self):
        with mock.patch.dict(os.environ, {"BREADCRUMBS_SUM_TEMPLATE": "gemma3"}):
            self.assertTrue(Llama(model="/x.gguf").prompt("t").endswith("<start_of_turn>model\n"))
        with self.assertRaises(ValueError):
            Llama(model="/x.gguf", template="nope")


@unittest.skipUnless(Llama().available(), "llama-completion or the default model file is missing")
class RealModelTest(Base):
    def test_a_real_summary_is_reproducible_from_its_record(self):
        sess, _ = self.session(12, size=240)
        llama = Llama(n=48)
        c = get_crumb(self.store, summarize(self.store, self.index, sess, llama))
        text = get_text(self.store, c.ranges[0][0]).decode("utf-8").strip()
        self.assertTrue(text)
        self.assertEqual(len(c.meta["model_sha256"]), 64)
        again = llama(get_text(self.store, c.meta["input"]).decode("utf-8"))
        self.assertEqual(clean(again), text)  # greedy decode: the record re-runs to the same text

    def tokens(self, prompt):
        """The prompt token count that llama.cpp reports for the runner's own argv."""
        p = subprocess.run(Llama(n=1).argv() + ["--verbose-prompt"], input=prompt.encode("utf-8"),
                           capture_output=True, timeout=120)
        m = re.search(rb"number of tokens in prompt = (\d+)", p.stderr)
        assert m is not None, p.stderr[-300:]
        return int(m.group(1))

    def test_the_model_sees_every_prompt_byte(self):
        # llama.cpp's -f drops one trailing newline; a chat template that ends in "assistant\n"
        # then reaches the model without its last token.
        self.assertEqual(self.tokens("Hello\n"), self.tokens("Hello") + 1)

    def test_backslashes_reach_the_model_unchanged(self):
        # llama.cpp turns a literal backslash-n into a newline unless --no-escape: agent logs hold
        # code, and meta.input must be the exact text the model saw.
        self.assertGreater(self.tokens("x" + "\\n" * 20), self.tokens("x" + "\n" * 20) + 10)


if __name__ == "__main__":
    unittest.main()
