"""The dials of each model (dials.py): target, fold share, fold cap and ratchet step reach the engine, and each
model's request options reach the OpenAI chat-completions wire."""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")

from breadcrumbs import ace, dials


def write_config(home, cfg):
    root = os.path.join(home, "breadcrumbs")
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return root


class OptionsTest(unittest.TestCase):
    def test_every_matching_glob_applies_in_file_order(self):
        cfg = {"models": {"*": {"ace_tokens": 100_000, "ace_ratchet": 1.2}, "*nemotron*": {"ace_tokens": 290_000},
                          "gpt-*": {"ace_tokens": 150_000}, "bad": "not a dict"}}
        self.assertEqual(dials.options(cfg, "nvidia/Nemotron-Ultra"), {"ace_tokens": 290_000, "ace_ratchet": 1.2})
        self.assertEqual(dials.options(cfg, "gpt-5"), {"ace_tokens": 150_000, "ace_ratchet": 1.2})
        self.assertEqual(dials.options({}, "gpt-5"), {})
        self.assertEqual(dials.options({"models": ["not", "a", "dict"]}, "gpt-5"), {})

    def test_a_missing_or_bad_file_gives_no_dials(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dials.config(tmp), {})
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(dials.config(tmp), {})
            with open(os.path.join(tmp, "config.json"), "w", encoding="utf-8") as f:
                f.write("[1, 2]")
            self.assertEqual(dials.config(tmp), {})

    def test_every_matching_glob_applies_to_request_options_in_file_order(self):
        cfg = {"models": {"*": {"temperature": 1.0, "top_p": 0.9}, "*nemotron*": {"temperature": 0.6},
                          "gpt-*": {"temperature": 0.2}, "bad": "not a dict"}}
        self.assertEqual(dials.options(cfg, "nvidia/Nemotron-Ultra"), {"temperature": 0.6, "top_p": 0.9})
        self.assertEqual(dials.options(cfg, "gpt-5"), {"temperature": 0.2, "top_p": 0.9})
        self.assertEqual(dials.options({}, "gpt-5"), {})
        self.assertEqual(dials.options({"models": ["not", "a", "dict"]}, "gpt-5"), {})


    def test_a_bool_dial_takes_a_bool_and_nothing_else(self):
        flag = dials.Dial("flag", False, "bool", None, None, "test", None, "a bool dial")
        self.assertIs(dials.valid(flag, True), True)
        self.assertIsNone(dials.valid(flag, "yes"))
        self.assertIsNone(dials.valid(flag, 1))

    def test_spec_keys_go_on_top_other_samplers_in_extra_body_and_null_removes(self):
        kw = {"model": "m", "messages": [], "temperature": 0.7, "extra_body": {"reasoning": {"effort": "high"}}}
        dials.apply(kw, {"temperature": 0.3, "seed": 7, "top_k": 20, "min_p": 0.05, "ace_tokens": 290000,
                         "extra_body": {"repetition_penalty": 1.1}})
        self.assertEqual(kw["temperature"], 0.3)
        self.assertEqual(kw["seed"], 7)
        self.assertEqual(kw["extra_body"], {"reasoning": {"effort": "high"}, "top_k": 20, "min_p": 0.05,
                                            "repetition_penalty": 1.1})
        self.assertNotIn("ace_tokens", kw)
        dials.apply(kw, {"temperature": None, "top_k": None, "min_p": None, "repetition_penalty": None,
                         "reasoning": None})
        self.assertNotIn("temperature", kw)
        self.assertNotIn("extra_body", kw)



class InstallTest(unittest.TestCase):
    """install() wraps the transport's build: a stand-in with Hermes's names and its legacy behavior (no
    temperature from the caller)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        class ChatCompletionsTransport:
            def build_kwargs(self, model, messages, tools=None, **params):
                return {"model": model, "messages": messages, "timeout": params.get("timeout")}

        mod = types.ModuleType("agent.transports.chat_completions")
        setattr(mod, "ChatCompletionsTransport", ChatCompletionsTransport)
        names = ("agent", "agent.transports", "agent.transports.chat_completions")
        saved = {n: sys.modules.get(n) for n in names}
        sys.modules.update({"agent": types.ModuleType("agent"), "agent.transports": types.ModuleType("agent.transports"),
                            "agent.transports.chat_completions": mod})

        def restore():
            for n in names:
                m = saved[n]
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m
        self.addCleanup(restore)
        self.T = ChatCompletionsTransport

    def test_the_options_of_the_model_reach_the_request_and_a_second_install_does_not_wrap_twice(self):
        write_config(self.tmp.name, {"models": {"local-*": {"temperature": 0.42, "top_k": 20}}})
        self.assertTrue(dials.install(lambda: self.tmp.name))
        wrapped = self.T.build_kwargs
        self.assertTrue(dials.install(lambda: self.tmp.name))
        self.assertIs(self.T.build_kwargs, wrapped)
        kw = self.T().build_kwargs(model="local-nemo", messages=[{"role": "user", "content": "hi"}], tools=None,
                                   timeout=30)
        self.assertEqual((kw["temperature"], kw["extra_body"], kw["timeout"]), (0.42, {"top_k": 20}, 30))
        self.assertEqual(self.T().build_kwargs("other", []), {"model": "other", "messages": [], "timeout": None})

    def test_an_edit_applies_to_the_next_request_and_a_bad_file_changes_nothing(self):
        dials.install(lambda: self.tmp.name)
        write_config(self.tmp.name, {"models": {"*": {"temperature": 0.5}}})
        self.assertEqual(self.T().build_kwargs("m", [])["temperature"], 0.5)
        write_config(self.tmp.name, {"models": {"*": {"temperature": 0.9}}})
        self.assertEqual(self.T().build_kwargs("m", [])["temperature"], 0.9)
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w") as f:
            f.write("{not json")
        self.assertNotIn("temperature", self.T().build_kwargs("m", []))

    def test_outside_hermes_install_does_nothing(self):
        sys.modules["agent.transports.chat_completions"] = None  # type: ignore[assignment]  # the import fails
        self.assertFalse(dials.install(lambda: self.tmp.name))

    def test_a_provider_that_logs_in_with_oauth_keeps_the_request_that_hermes_built(self):
        """Its terms can forbid a changed request (Nov 9 2026): only the open API endpoints change. The
        profile names its auth type; a provider with no profile (Hermes's legacy path) has a row in the auth
        registry of hermes_cli.auth."""
        auth = types.ModuleType("hermes_cli.auth")
        setattr(auth, "PROVIDER_REGISTRY", {"oldlogin": types.SimpleNamespace(auth_type="oauth_device_code"),
                                            "oldkey": types.SimpleNamespace(auth_type="api_key")})
        saved = {n: sys.modules.get(n) for n in ("hermes_cli", "hermes_cli.auth")}
        sys.modules.update({"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.auth": auth})
        self.addCleanup(lambda: [sys.modules.pop(n, None) if m is None else sys.modules.__setitem__(n, m)
                                 for n, m in saved.items()])
        write_config(self.tmp.name, {"models": {"*": {"temperature": 0.42}}})
        dials.install(lambda: self.tmp.name)
        P = types.SimpleNamespace
        for params in ({"provider_profile": P(name="nous", auth_type="oauth_device_code")},
                       {"provider_profile": P(name="qwen-oauth", auth_type="oauth_external")},
                       {"provider_profile": P(name="m", auth_type="api_key", registry_auth_type="oauth_minimax")},
                       {"provider_name": "OldLogin"}):
            self.assertNotIn("temperature", self.T().build_kwargs("m", [], **params), params)
        for params in ({"provider_profile": P(name="openrouter", auth_type="api_key")}, {"provider_name": "oldkey"},
                       {"provider_name": "custom"}, {}):
            self.assertEqual(self.T().build_kwargs("m", [], **params)["temperature"], 0.42, params)


class DialTest(unittest.TestCase):
    """The ACE dials of the model: target, fold share, fold cap and ratchet step, from the same entries."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        write_config(self.tmp.name, {"ace_tokens": 100_000, "models": {
            "big-*": {"temperature": 0.6, "ace_tokens": 290_000, "ace_fold_to": 0.8, "ace_ratchet": 2.0},
            "big-x": {"ace_fold_cap": 5}, "big-y": {"ace_ratchet": True}}})  # out of range, not a number: defaults

    def engine(self, model, target=None):
        e = ace.Ace(target_tokens=target)
        e.on_session_start("s-dial", hermes_home=self.tmp.name)
        self.addCleanup(e.close)
        e.update_model(model, 1_000_000)
        self.assertTrue(e._open())
        return e

    def test_a_model_takes_its_entries_and_a_switch_takes_the_new_model_s(self):
        e = self.engine("big-x")
        self.assertEqual((e.target_tokens, e.fold_to, e.fold_cap, e.step), (290_000, 0.8, ace.FOLD_CAP, 2.0))
        e.update_model("small", 1_000_000)
        self.assertEqual((e.target_tokens, e.fold_to, e.fold_cap, e.step),
                         (100_000, ace.FOLD_TO, ace.FOLD_CAP, ace.RATCHET))

    def test_a_given_target_stays(self):
        e = self.engine("big-x", target=50_000)
        self.assertEqual((e.target_tokens, e.fold_to), (50_000, 0.8))

    def test_a_later_bad_value_gives_the_default(self):
        self.assertEqual(self.engine("big-y").step, ace.RATCHET)

    def test_a_fold_aims_at_the_dial(self):
        e = self.engine("big-x")
        self.assertEqual(min(e.fold_cap, e.fold_to / e.gain), 0.8)
        e.gain = 0.5  # the cap holds a low gain
        self.assertEqual(min(e.fold_cap, e.fold_to / e.gain), ace.FOLD_CAP)

class StatusTest(unittest.TestCase):
    """status_lines shows each chat's size against its target, so that a size over the target is seen."""

    def test_the_newest_line_of_each_chat_and_a_mark_when_it_is_over(self):
        with tempfile.TemporaryDirectory() as root:
            row = ("%s pid=1 sess=%s act=%s in=%d out=%d last=1 base=100 tpb=0.3000 r=0.3000 folds=%d layer=0 "
                   "target=%d gain=0.9000\n")
            with open(os.path.join(root, "ace.log"), "w", encoding="utf-8") as f:
                f.write(row % ("2026-11-10T08:00:00", "chat-a", "pass", 100_000, 100_000, 0, 290_000))
                f.write(row % ("2026-11-10T08:10:00", "chat-b", "pass", 427_247, 427_247, 0, 290_000))
                f.write(row % ("2026-11-10T08:20:00", "chat-a", "fold", 240_000, 240_000, 1, 290_000))
                f.write("2026-11-10T08:30:00 pid=1 sess=chat-c act=pass in=9")  # no line end: not a whole line
            lines = ace.status_lines(root)
            self.assertEqual(len(lines), 2)  # chat-b is newer than chat-a's first line; chat-c is not whole
            self.assertIn("chat-a", lines[0]); self.assertIn("fold", lines[0]); self.assertIn("82%", lines[0])
            self.assertNotIn("OVER", lines[0])
            self.assertIn("chat-b", lines[1]); self.assertIn("OVER by 137,247", lines[1]); self.assertIn("147%", lines[1])
            with open(os.path.join(root, "ace.log"), "a", encoding="utf-8") as f:  # a keep: the request was large, ACE sent less
                f.write("\n" + row % ("2026-11-10T08:40:00", "chat-d", "keep", 900_000, 250_000, 1, 290_000))
            d = ace.status_lines(root)[0]
            self.assertIn("chat-d", d); self.assertIn("in   900,000", d); self.assertIn("sent   250,000", d)
            self.assertNotIn("OVER", d)
            self.assertEqual(ace.status_lines(os.path.join(root, "none")), [])

class RegistryTest(unittest.TestCase):
    """DIALS is the one table of the dials: the engine reads it, the command shows it, README holds it."""

    def test_the_defaults_are_the_constants_of_the_code(self):
        from breadcrumbs.horizon import Controller
        d = {x.key: x.default for x in dials.DIALS}
        self.assertEqual((d["ace_tokens"], d["ace_fold_to"], d["ace_fold_cap"], d["ace_ratchet"]),
                         (ace.TARGET_TOKENS, ace.FOLD_TO, ace.FOLD_CAP, ace.RATCHET))
        c = Controller()
        self.assertEqual((d["budget_min"], d["budget_max"], (d["overflow_low"], d["overflow_high"])), (c.lo, c.hi, c.band))
        self.assertEqual(len(dials.BY_KEY), len(dials.DIALS))
        for x in dials.DIALS:
            self.assertIsNotNone(dials.valid(x, x.default) if x.kind != "bool" else True, x.key)

    def test_the_order_is_default_then_config_then_model_then_environment(self):
        cfg = {"neighbor": 0.2, "models": {"big-*": {"neighbor": 0.4}, "big-x": {"neighbor": 0.6}}}
        get = lambda model, env: next(r for r in dials.resolve(cfg, model, env) if r.dial.key == "neighbor")
        self.assertEqual((get("other", {}).value, get("other", {}).source), (0.2, "config"))
        self.assertEqual((get("big-y", {}).value, get("big-y", {}).source), (0.4, 'models["big-*"]'))
        self.assertEqual((get("big-x", {}).value, get("big-x", {}).source), (0.6, 'models["big-x"]'))  # file order
        self.assertEqual((get("big-x", {"BREADCRUMBS_NEIGHBOR": "0.1"}).value,
                          get("big-x", {"BREADCRUMBS_NEIGHBOR": "0.1"}).source), (0.1, "env BREADCRUMBS_NEIGHBOR"))
        self.assertEqual(dials.value({}, None, "neighbor", {}), 0.3)

    def test_a_value_that_is_not_valid_gives_the_default_and_is_named(self):
        cfg = {"ace_ratchet": 9.0, "ace_tokens": True, "intercept": "yes", "turn_factor": -1, "ace_diverse": 2.5}
        rows = {r.dial.key: r for r in dials.resolve(cfg, None, {})}
        for key in cfg:
            self.assertEqual((rows[key].value, rows[key].source), (dials.BY_KEY[key].default, "default"), key)
            self.assertEqual(len(rows[key].problems), 1, key)
            self.assertIn("default is used", rows[key].problems[0])
        self.assertIn("a number from 1 to 4", rows["ace_ratchet"].problems[0])

    def test_an_environment_variable_that_is_not_valid_is_ignored_and_named(self):
        row = next(r for r in dials.resolve({"neighbor": 0.2}, None, {"BREADCRUMBS_NEIGHBOR": "lots"}) if r.dial.key == "neighbor")
        self.assertEqual((row.value, row.source), (0.2, "config"))
        self.assertIn("BREADCRUMBS_NEIGHBOR='lots' is not a number from 0 to 1; ignored", row.problems)

    def test_a_key_that_is_no_dial_is_named(self):
        cfg = {"ace_token": 1, "hive": {}, "slot": "x", "models": {"*": {"ace_ratchett": 2, "ace_tokens": 5}, "bad": 3}}
        self.assertEqual(dials.unknown(cfg), ["config ace_token: no such dial", 'models["*"] ace_ratchett: no such dial',
                                              'models["bad"]: not an object; ignored'])

    def test_the_command_shows_every_dial_its_source_and_what_was_ignored(self):
        import contextlib
        import io
        from breadcrumbs import cli
        with tempfile.TemporaryDirectory() as root:
            write_config(os.path.dirname(root), {})  # (a stray file in the parent is not read)
            with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"ace_tokens": 290_000, "neighbour": 1, "models": {"x-*": {"ace_ratchet": 1.0}}}, f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--home", root, "dials", "--model", "x-1"]), 0)
        text = out.getvalue()
        for d in dials.DIALS:
            self.assertIn(d.key, text)
        for name, _default, _doc in dials.KNOBS:
            self.assertIn(name, text)
        self.assertRegex(text, r"ace_tokens\s+290,000\s+default 200,000\s+<- config")
        self.assertRegex(text, r'ace_ratchet\s+1\s+default 1.45\s+<- models\["x-\*"\]')
        self.assertIn("config neighbour: no such dial", text)

    def test_readme_holds_the_table_of_the_registry(self):
        """The README table is dials.markdown(): a dial cannot change without the README."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md"), encoding="utf-8") as f:
            self.assertIn(dials.markdown(), f.read())


class ProviderDialsTest(unittest.TestCase):
    """The recall and controller dials of a profile reach the provider; the environment wins; the controller keeps
    the budget it saved."""

    def provider(self, home, session="s1"):
        from breadcrumbs.hermes import BreadcrumbsProvider
        p = BreadcrumbsProvider()
        p.initialize(session, hermes_home=home, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        return p

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        write_config(self.tmp.name, {"neighbor": 0.0, "recall_share": 0.5, "turn_factor": 3, "age_half_days": 10,
                                     "idle_half_days": 2, "prefetch_tokens": 123, "budget_min": 50, "budget_max": 900,
                                     "overflow_low": 0.1, "overflow_high": 0.2})
        for name in ("BREADCRUMBS_NEIGHBOR", "BREADCRUMBS_RECALL_SHARE", "BREADCRUMBS_TURN_FACTOR",
                     "BREADCRUMBS_PREFETCH_TOKENS", "BREADCRUMBS_AGE_HALF_DAYS", "BREADCRUMBS_IDLE_HALF_DAYS"):
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop(name, None)

    def test_the_profile_s_dials_reach_the_provider(self):
        p = self.provider(self.tmp.name)
        self.assertEqual((p.neighbor, p.recall_share, p.turn_factor), (0.0, 0.5, 3.0))
        self.assertEqual((p.age_half, p.idle_half), (10 * 86400, 2 * 86400))
        self.assertEqual((p.ctl.budget, p.ctl.lo, p.ctl.hi, p.ctl.band), (123, 50, 900, (0.1, 0.2)))

    def test_the_environment_wins_over_the_profile(self):
        os.environ["BREADCRUMBS_NEIGHBOR"] = "0.25"
        os.environ["BREADCRUMBS_PREFETCH_TOKENS"] = "77"
        p = self.provider(self.tmp.name)
        self.assertEqual((p.neighbor, p.ctl.budget), (0.25, 77))

    def test_the_controller_keeps_the_budget_it_saved(self):
        p = self.provider(self.tmp.name)
        p.ctl.budget = 456
        p._save_horizon()
        p.shutdown()
        q = self.provider(self.tmp.name, "s2")
        self.assertEqual(q.ctl.budget, 456)  # not prefetch_tokens: that is the first budget only
        write_config(self.tmp.name, {"budget_max": 400})
        q.shutdown()
        r = self.provider(self.tmp.name, "s3")
        self.assertEqual(r.ctl.budget, 400)  # but always inside the range of the profile

    def test_a_profile_with_no_dials_has_the_defaults(self):
        with tempfile.TemporaryDirectory() as home:
            p = self.provider(home)
            self.assertEqual((p.neighbor, p.recall_share, p.turn_factor, p.ctl.budget), (0.3, 0.15, 0.0, 300))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
