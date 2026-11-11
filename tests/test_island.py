"""Islands (breadcrumbs/island.py): one codebase for each profile, the /island handoff, the checks and the
automatic offer.

Each Sandbox test builds a home folder in a temp folder: repositories (a plain one, a linked worktree, a
submodule, a nested one, a hidden one, one outside the home folder, Hermes's own) and a Hermes root with
profiles. The handoff runs with a recorded Hermes (FakeHermes): it does what `hermes profile create NAME
--clone` and `hermes -p NAME config set terminal.cwd ROOT` do to the files that islands read. The real
Hermes parser checks that it accepts the two command lines (skipped where Hermes is not importable).
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from breadcrumbs import ace, cli, island

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class FakeHermes:
    """Hermes's CLI as islands see it: `profile create NAME --clone-from SOURCE` copies that profile (its
    config.yaml and plugins/), `-p NAME config set terminal.cwd V` writes the terminal block."""

    def __init__(self, hermes: Path, fail: str = ""):
        self.hermes, self.fail, self.calls = hermes, fail, []

    def __call__(self, args, home_dir):
        self.calls.append((list(args), home_dir))
        if self.fail and self.fail in args:
            return 1, "Error: something failed\nsecond line"
        if args[:2] == ["profile", "create"]:
            dst = self.hermes / "profiles" / args[2]
            if dst.exists():
                return 1, "Error: Profile '%s' already exists" % args[2]
            dst.mkdir(parents=True)
            source = args[args.index("--clone-from") + 1]
            src = self.hermes if source == "default" else self.hermes / "profiles" / source
            shutil.copy(src / "config.yaml", dst / "config.yaml")
            if (src / "plugins").is_dir():
                shutil.copytree(src / "plugins", dst / "plugins")
            return 0, "Profile '%s' created" % args[2]
        if args[0] == "-p" and args[2:5] == ["config", "set", "terminal.cwd"]:
            cfg = self.hermes / "profiles" / args[1] / "config.yaml"
            text = cfg.read_text() if cfg.exists() else ""
            text = re.sub(r"^terminal:[ \t]*\n(?:[ \t]+.*\n?)*", "", text, flags=re.M)
            cfg.write_text(text.rstrip("\n") + ("\n" if text.strip() else "") + "terminal:\n  cwd: %s\n" % args[5])
            return 0, ""
        return 2, "unknown command"


class Sandbox(unittest.TestCase):
    _real_chat_folder = staticmethod(island.chat_folder)

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="bc-island-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.hermes = self.home / ".hermes"
        h, w = self.home, self.home / "work"
        for repo in ("a", "b", "c", "gamma", "named", "Odd.Name_Repo", "nested/outer", "nested/outer/inner"):
            (w / repo / ".git").mkdir(parents=True)
        (w / "b" / "src").mkdir()
        (w / "plain").mkdir(parents=True)
        write(w / "a" / "sub" / ".git", "gitdir: ../.git/modules/sub\n")  # a submodule of a
        write(w / "a-wt" / ".git", "gitdir: %s\n" % (w / "a" / ".git" / "worktrees" / "a-wt"))  # a worktree of a
        (h / ".pyenv" / "repo" / ".git").mkdir(parents=True)  # hidden: a tool, not a codebase
        (self.tmp / "outside" / "repo" / ".git").mkdir(parents=True)  # outside the home folder
        (self.hermes / "hermes-agent" / ".git").mkdir(parents=True)  # Hermes's own files
        write(self.hermes / "config.yaml", 'terminal:\n  cwd: "."  # For local backend\n')
        profiles = self.hermes / "profiles"
        write(profiles / "alpha" / "config.yaml", "model:\n  default: x\nterminal:\n  backend: local\n  cwd: ~/work/a\n")
        write(profiles / "beta" / "config.yaml", 'terminal:\n  cwd: "%s"\n' % (w / "b" / "src"))
        write(profiles / "dup" / "config.yaml", "terminal:\n  cwd: %s\n" % (w / "a"))  # alpha wins (name order)
        write(profiles / "gamma" / "config.yaml", 'terminal:\n  cwd: "."\nmemory:\n  provider: holographic\n')
        write(profiles / "src-c" / "config.yaml", "terminal:\n  cwd: .\n")
        for p in (mock.patch.dict(os.environ, {"HOME": str(h)}), mock.patch.object(island, "HOME", str(h)),
                  mock.patch.object(island, "hermes_root", return_value=str(self.hermes)),
                  mock.patch.object(island, "chat_folder", return_value="")):  # no folder pinned by Hermes
            p.start()
            self.addCleanup(p.stop)
        self.run_ = FakeHermes(self.hermes)

    def w(self, rel):
        return str(self.home / "work" / rel)

    def handoff(self, path, name=None, current="gamma", run=None):
        return island.handoff(path, name, current=current, run=run or self.run_, home=str(self.home), hermes=str(self.hermes))

    def islands(self):
        return island.islands(str(self.home), str(self.hermes))


class CodebaseTest(Sandbox):
    CASES = {
        "a/new_file.py": "a",  # a file that does not exist yet
        "a/sub/x.c": "a",  # a submodule counts as its main repository
        "a-wt/x": "a",  # so does a linked worktree
        "b/src": "b",
        "nested/outer/inner/f": "nested/outer/inner",  # the nearest repository
        "nested/outer/g": "nested/outer",
        "plain": None,
    }

    def test_the_codebase_of_a_path(self):
        for rel, want in self.CASES.items():
            got = island.root_of(self.w(rel), str(self.home), str(self.hermes))
            self.assertEqual(got, self.w(want) if want else None, rel)
        for path in (self.home / ".pyenv" / "repo", self.tmp / "outside" / "repo", self.hermes / "hermes-agent", self.hermes,
                     self.home):
            self.assertIsNone(island.root_of(str(path), str(self.home), str(self.hermes)), path)

    def test_the_defaults_are_the_home_folder_and_the_hermes_root(self):
        self.assertEqual(island.root_of(self.w("a-wt/x")), self.w("a"))
        self.assertIsNone(island.root_of(str(self.hermes / "hermes-agent" / "run.py")))
        self.assertEqual(island.root_of("~/work/b/src/x.py"), self.w("b"))
        self.assertEqual(island.islands(), self.islands())

    def test_settings_are_ace_settings(self):
        self.assertIs(island.setting, ace.setting)  # one reading of config.yaml for the whole plugin
        cfg = self.tmp / "commented"  # Hermes's template puts `# ====` lines at column 0 inside `memory:`
        write(cfg / "config.yaml", "memory:\n  memory_enabled: true\n\n# =====\n# Memory provider\n"
                                   "  provider: breadcrumbs\n# =====\nother:\n  provider: nope\nterminal:\n  cwd: /x\n")
        self.assertEqual(island.setting(str(cfg), "memory", "provider"), "breadcrumbs")
        self.assertIsNone(island.setting(str(cfg), "terminal", "provider"))  # a top-level key ends the block
        self.assertEqual(island.setting(str(self.hermes / "profiles" / "alpha"), "terminal", "cwd"), "~/work/a")
        self.assertEqual(island.setting(str(self.hermes), "terminal", "cwd"), ".")
        self.assertIsNone(island.setting(str(self.tmp / "nowhere"), "terminal", "cwd"))

    def test_the_island_of_each_profile(self):
        self.assertEqual(self.islands(), {self.w("a"): "alpha", self.w("b"): "beta"})  # dup: alpha came first

    @unittest.skipUnless(os.name == "nt", "~\\x and C:/x are forms of a Windows path; on POSIX a backslash is a "
                                          "character of a file name")
    def test_the_windows_forms_of_a_path(self):
        b = self.w("b")
        for path in ("~\\work\\b\\src\\x.py", b.replace("\\", "/") + "/src/x.py", b + "\\\\src\\\\x.py"):
            self.assertEqual(island.root_of(path, str(self.home), str(self.hermes)), b, path)
        r = self.handoff("~\\work\\a-wt")
        self.assertIn("`~/work/a` has its own profile: `alpha`", r.text)  # a message shows / on every platform

    def test_a_profile_name_comes_from_the_repository_folder(self):
        self.assertEqual(island.profile_name(self.w("Odd.Name_Repo")), "odd-name_repo")
        self.assertEqual(island.profile_name(self.w("named")), "named")
        self.assertEqual(island.profile_name("/x/---"), "island")
        try:
            from hermes_cli.profiles import validate_profile_name
            validate_profile_name("hermes")
        except ImportError:
            self.skipTest("no hermes_cli here")
        except ValueError:  # reserved by Hermes
            self.assertEqual(island.profile_name("/x/hermes"), "hermes-island")


class ChildEnvironmentTest(unittest.TestCase):
    """The hermes child that /island starts: stdin closed, 300 s, HERMES_HOME of the profile, and never an
    environment that skips approval or asks nothing."""

    def test_the_child_environment_drops_yolo_and_noninteractive_and_keeps_the_rest(self):
        env = {"HERMES_YOLO_MODE": "1", "HERMES_NONINTERACTIVE": "1", "HERMES_HOME": "/elsewhere", "KEEP_ME": "yes"}
        done = mock.Mock(returncode=0, stdout="ok", stderr="")
        with mock.patch.dict(os.environ, env), mock.patch.object(island.subprocess, "run", return_value=done) as run:
            self.assertEqual(island.run_hermes(["config", "set", "terminal.cwd", "/x"], "/profile"), (0, "ok"))
            self.assertEqual(os.environ["HERMES_YOLO_MODE"], "1")  # our own environment is not touched
        child = run.call_args.kwargs["env"]
        self.assertNotIn("HERMES_YOLO_MODE", child)
        self.assertNotIn("HERMES_NONINTERACTIVE", child)
        self.assertEqual((child["HERMES_HOME"], child["KEEP_ME"]), ("/profile", "yes"))
        self.assertEqual((run.call_args.kwargs["timeout"], run.call_args.kwargs["stdin"]), (300, island.subprocess.DEVNULL))


class HandoffTest(Sandbox):
    def test_a_new_codebase_gets_a_clone_of_this_profile_and_its_cwd(self):
        (self.home / "work" / "named" / "deep").mkdir()
        r = self.handoff(self.w("named/deep"))
        self.assertTrue(r.ok, r.text)
        root = self.w("named")
        self.assertEqual(self.run_.calls, [
            (["profile", "create", "named", "--clone-from", "gamma", "--no-alias", "--description",
              "The island of ~/work/named (one codebase): made by /island (breadcrumbs)."], str(self.hermes / "profiles" / "gamma")),
            (["-p", "named", "config", "set", "terminal.cwd", root], str(self.hermes / "profiles" / "named")),
        ])
        self.assertEqual(r.calls, [c for c, _ in self.run_.calls])
        self.assertEqual(self.islands()[root], "named")
        self.assertIn("Continue that work there: `hermes -p named`", r.text)
        self.assertIn("It is a clone of `gamma`", r.text)
        self.assertIn("hermes -p named config set memory.provider breadcrumbs", r.text)  # gamma has no breadcrumbs
        # The next call finds the island and changes nothing.
        again = self.handoff(root)
        self.assertTrue(again.ok)
        self.assertEqual(len(self.run_.calls), 2)
        self.assertIn("has its own profile: `named`. Continue that work there: `hermes -p named`", again.text)

    def test_a_codebase_with_an_island_keeps_it(self):
        r = self.handoff(self.w("a-wt"))
        self.assertTrue(r.ok)
        self.assertEqual(self.run_.calls, [])
        self.assertIn("`~/work/a` has its own profile: `alpha`", r.text)
        mine = self.handoff(self.w("a"), current="alpha")
        self.assertIn("is the island of this profile (`alpha`)", mine.text)
        moved = self.handoff(self.w("a"), "beta")
        self.assertFalse(moved.ok)
        self.assertIn("/island does not move an island", moved.text)
        self.assertEqual(self.run_.calls, [])

    def test_a_profile_without_an_island_gets_the_island_when_no_name_is_given(self):
        r = self.handoff(self.w("gamma"))
        self.assertTrue(r.ok)
        self.assertEqual(self.run_.calls, [(["-p", "gamma", "config", "set", "terminal.cwd", self.w("gamma")],
                                            str(self.hermes / "profiles" / "gamma"))])
        self.assertIn("is now the island of profile `gamma`", r.text)
        # src-<name>: an existing profile for the codebase gets the island before a second one is made.
        c = self.handoff(self.w("c"))
        self.assertTrue(c.ok)
        self.assertEqual(self.run_.calls[-1][0][:2], ["-p", "src-c"])
        self.assertNotIn("create", [x for call, _ in self.run_.calls for x in call])

    def test_a_named_profile_with_no_island_becomes_the_island(self):
        r = self.handoff(self.w("gamma"), "Gamma")  # Hermes's form: lower case
        self.assertTrue(r.ok, r.text)
        self.assertEqual([c for c, _ in self.run_.calls], [["-p", "gamma", "config", "set", "terminal.cwd", self.w("gamma")]])
        self.assertEqual(self.islands()[self.w("gamma")], "gamma")
        self.assertIn("is now the island of profile `gamma`", r.text)
        self.assertIn("memory:\n  provider: holographic", (self.hermes / "profiles" / "gamma" / "config.yaml").read_text())

    def test_a_name_that_holds_another_island_is_refused(self):
        r = self.handoff(self.w("c"), "alpha")
        self.assertFalse(r.ok)
        self.assertEqual(self.run_.calls, [])
        self.assertIn("Profile `alpha` is the island of `~/work/a`", r.text)
        bad = self.handoff(self.w("c"), "default")
        self.assertFalse(bad.ok)
        self.assertIn("not an island", bad.text)

    def test_a_profile_that_shares_an_island_can_get_its_own(self):
        r = self.handoff(self.w("c"), "dup")  # dup's terminal.cwd is in ~/work/a, whose island is alpha's
        self.assertTrue(r.ok, r.text)
        self.assertEqual(self.islands()[self.w("c")], "dup")
        self.assertEqual(island.checks(str(self.home), str(self.hermes)), [])

    def test_what_is_no_codebase_gets_no_profile(self):
        for path in (self.w("plain"), str(self.home / ".pyenv" / "repo"), str(self.tmp / "outside" / "repo"),
                     str(self.hermes / "hermes-agent")):
            r = self.handoff(path)
            self.assertFalse(r.ok, path)
            self.assertIn("is in no codebase", r.text)
        self.assertIn("No file or folder", self.handoff(self.w("missing")).text)
        self.assertEqual(self.run_.calls, [])

    def test_a_failed_hermes_command_is_reported_and_stops(self):
        run = FakeHermes(self.hermes, fail="create")
        r = self.handoff(self.w("named"), run=run)
        self.assertFalse(r.ok)
        self.assertEqual(len(run.calls), 1)
        self.assertIn("`hermes profile create named` failed (exit 1):", r.text)
        self.assertIn("  second line", r.text)
        run = FakeHermes(self.hermes, fail="terminal.cwd")
        r = self.handoff(self.w("named"), run=run)
        self.assertFalse(r.ok)
        self.assertIn("config set terminal.cwd` failed", r.text)


class OfferTest(Sandbox):
    def test_the_offer_names_the_profile_and_skips_what_it_has(self):
        lines = island.offer("gamma", str(self.hermes))
        self.assertIn("the layer does not change, so the prompt cache holds", lines[0])
        self.assertIn("a plain window of the newest messages holds more exact text", lines[0])  # the limit, in full
        self.assertNotRegex(lines[0], r"\d[\d.,]*(%| times)")  # no multiplier and no percentage: the bench is not run again
        self.assertEqual(lines[1:], ["  hermes -p gamma plugins install breadcrumbs --enable",
                                     "  hermes -p gamma config set memory.provider breadcrumbs",
                                     "  hermes -p gamma config set context.engine breadcrumbs"])
        write(self.hermes / "profiles" / "beta" / "plugins" / "bc" / "plugin.yaml", "name: breadcrumbs\n")
        self.assertNotIn("plugins install", "\n".join(island.offer("beta", str(self.hermes))))
        write(self.hermes / "profiles" / "beta" / "config.yaml", "memory:\n  provider: breadcrumbs\n")
        self.assertEqual(island.offer("beta", str(self.hermes)), [])

    def test_status_shows_this_profile_the_islands_and_the_offer(self):
        r = island.status("gamma", str(self.home), str(self.hermes))
        self.assertTrue(r.text.startswith("breadcrumbs islands: one codebase for each profile."), r.text)
        self.assertIn("This profile (`gamma`): no island", r.text)
        self.assertIn("  ~/work/a -> alpha", r.text)
        self.assertIn("  ~/work/b -> beta", r.text)
        self.assertIn("hermes -p gamma config set context.engine breadcrumbs", r.text)
        self.assertIn("its island is `~/work/a`", island.status("alpha", str(self.home), str(self.hermes)).text)

    def test_breadcrumbs_alone_gives_the_islands(self):
        # No detection of, and no ask for, another plugin: /island, the handoff, the checks and the offer are here.
        source = (ROOT / "breadcrumbs" / "island.py").read_text(encoding="utf-8")
        self.assertNotIn("spacegraph", source.lower())
        for name in ("spacegraph", "get_spacegraph", "enabled", "ISLAND_CMD", "OFFER_HOOK", "SPACEGRAPH_REPO"):
            self.assertFalse(hasattr(island, name), name)


class RegisterTest(Sandbox):
    def test_register_adds_the_hooks_the_slash_command_and_the_cli_command(self):
        got = {}

        class Ctx:
            def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
                got.setdefault("commands", {})[name] = (name, handler, description, args_hint)

            def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
                got["cli"] = (name, setup_fn, handler_fn)

            def register_hook(self, hook_name, callback, *, hold_stream=False):
                got.setdefault("hooks", []).append(hook_name)

        self.assertIsNotNone(island.register(Ctx()))
        self.assertEqual(list(got["commands"]), ["island", "viz"])
        viz_handler = got["commands"]["viz"][1]
        self.assertIn("not for messaging chats (telegram)", viz_handler("", platform="telegram"))
        with mock.patch.object(island, "open_viz", return_value="open") as opened:
            self.assertEqual(viz_handler("", session_id="s9", platform="desktop"), "open")
        opened.assert_called_once_with(session_id="s9")  # the chat: a no-vis session opens nothing
        name, handler, description, hint = got["commands"]["island"]
        self.assertEqual((name, hint), ("island", "[path]"))
        self.assertEqual(handler("status"), island.command("status"))
        self.assertIn("not for messaging chats (telegram)", handler("status", platform="telegram"))
        self.assertEqual(handler("status", platform="cli"), island.command("status"))
        self.assertIn("own profile", description)
        self.assertEqual(got["cli"][0], "islands")
        self.assertIs(got["cli"][2], island.cli_run)
        parser = argparse.ArgumentParser(prog="hermes islands")
        got["cli"][1](parser)
        args = parser.parse_args(["island", "~/work/x", "xname"])
        self.assertEqual((args.islands_action, args.path, args.name), ("island", "~/work/x", "xname"))
        self.assertEqual(parser.parse_args(["status"]).islands_action, "status")
        self.assertTrue(parser.parse_args(["status", "--all"]).all)
        self.assertEqual(got["hooks"], ["on_session_start", "post_tool_call", "transform_llm_output"])  # the automatic offer

    def test_register_gives_the_slash_command_its_completer_where_hermes_has_the_argument(self):
        got = {}

        class Ctx:
            def register_command(self, name, handler, description="", args_hint="", argument_mode=None, completer=None):
                got.setdefault("completer", completer)  # /island is the first command; /viz has no argument

        island.register(Ctx())
        self.assertIs(got["completer"], island.complete)

    def test_the_completer_probes_the_launch_folder_for_its_codebase_and_its_island(self):
        gamma = self.hermes / "profiles" / "gamma"
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(gamma), "TERMINAL_CWD": self.w("named") + "/src"}):
            self.assertEqual(island.complete("")[0], (".", "~/work/named: no island"))
            self.assertEqual(island.complete(".")[0][0], ".")
            self.assertEqual([t for t, _ in island.complete("")[1:]], ["status", "--all"])
            island.save(str(gamma), {"s": {"roots": {self.w("a"): 3}, "t": 1}})
            rows = island.complete("")
            self.assertEqual(rows[1], ("~/work/a", "this chat's codebase, the island of `alpha`"))
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(gamma), "TERMINAL_CWD": str(self.home)}), \
                mock.patch("os.getcwd", return_value=str(self.home)):
            self.assertEqual(island.complete("")[0], (".", "the launch folder is in no codebase"))
            self.assertEqual(island.complete("a b"), [])
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(gamma), "TERMINAL_CWD": self.w("a")}):
            self.assertEqual(island.complete("")[0], (".", "~/work/a: the island of `alpha`"))
            with mock.patch.object(island, "run_hermes", self.run_):
                self.assertIn("hermes -p alpha", island.command("."))  # "." is the default argument

    def test_a_hermes_with_only_hooks_gets_the_hooks_and_one_with_none_gets_nothing(self):
        hooks = []

        class HookOnly:
            def register_hook(self, name, callback):
                hooks.append(name)

        self.assertIsNotNone(island.register(HookOnly()))
        self.assertEqual(hooks, ["on_session_start", "post_tool_call", "transform_llm_output"])
        self.assertIsNone(island.register(object()))

    def test_the_hooks_never_raise(self):
        hooks = {}

        class Ctx:
            def register_hook(self, name, callback):
                hooks[name] = callback

        island.register(Ctx())
        with mock.patch.object(island.Tracker, "post_tool_call", side_effect=RuntimeError("x")), \
                mock.patch.object(island.Tracker, "transform_llm_output", side_effect=RuntimeError("x")):
            self.assertIsNone(hooks["post_tool_call"](tool_name="t", args={"path": "/x"}, session_id="s"))
            self.assertIsNone(hooks["transform_llm_output"](response_text="r", session_id="s"))

    def test_the_cli_command_runs_the_status_the_report_and_the_handoff(self):
        parser = argparse.ArgumentParser(prog="hermes islands")
        island.cli_setup(parser)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "gamma")}), \
                mock.patch.object(island, "run_hermes", self.run_):
            for argv, code, words in ((["status"], 0, "This profile (`gamma`)"), ([], 0, "This profile (`gamma`)"),
                                      (["status", "--all"], 0, "breadcrumbs islands of all profiles"),
                                      (["island", self.w("named")], 0, "`hermes -p named`"),
                                      (["island", self.w("plain")], 1, "is in no codebase")):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(island.cli_run(parser.parse_args(argv)), code, argv)
                self.assertIn(words, out.getvalue(), argv)

    def test_hermes_accepts_the_two_command_lines(self):
        try:
            from hermes_cli.subcommands.config import build_config_parser
            from hermes_cli.subcommands.profile import build_profile_parser
        except ImportError:
            self.skipTest("no hermes_cli here")
        r = self.handoff(self.w("named"))
        create, set_cwd = r.calls
        parser = argparse.ArgumentParser(prog="hermes")
        sub = parser.add_subparsers(dest="command")
        build_profile_parser(sub, cmd_profile=lambda a: 0)
        build_config_parser(sub, cmd_config=lambda a: 0)
        a = parser.parse_args(create)
        self.assertEqual((a.profile_name, a.clone_from, a.clone, a.no_alias), ("named", "gamma", False, True))
        self.assertTrue(a.description.startswith("The island of ~/work/named"))
        self.assertEqual(set_cwd[:2], ["-p", "named"])  # Hermes reads -p before its parser (main._apply_profile_override)
        b = parser.parse_args(set_cwd[2:])
        self.assertEqual(vars(b).get("command"), "config")
        self.assertEqual(set_cwd[2:], ["config", "set", "terminal.cwd", self.w("named")])

    def test_a_gateway_profile_scope_wins_over_the_process_environment(self):
        try:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        except ImportError:
            self.skipTest("no hermes_constants here")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "alpha")}), \
                mock.patch.object(island, "run_hermes", self.run_):
            token = set_hermes_home_override(str(self.hermes / "profiles" / "gamma"))
            try:
                self.assertIn("This profile (`gamma`)", island.command("status"))
                island.command(self.w("named"))
            finally:
                reset_hermes_home_override(token)
            self.assertEqual(self.run_.calls[0][0][3:5], ["--clone-from", "gamma"])
            self.assertIn("This profile (`alpha`)", island.command("status"))

    def test_the_slash_command_parses_its_words(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "gamma")}), \
                mock.patch.object(island, "run_hermes", self.run_):
            self.assertIn("This profile (`gamma`)", island.command("status"))
            self.assertIn("breadcrumbs islands of all profiles", island.command("--all"))
            self.assertIn("Usage", island.command("a b c"))
            with mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("a")}):  # no PATH: the launch folder
                self.assertIn("has its own profile: `alpha`", island.command(""))
            self.assertIn("has its own profile: `alpha`", island.command("'%s'" % self.w("a")))
            self.assertIn("hermes -p named", island.command(self.w("named")))

    def test_a_gateway_chat_runs_the_command_in_its_own_folder(self):
        # gatewayd runs the CLI, TUI and desktop chats: a plugin command gets platform "local", in the gateway's
        # process folder; the chat's folder (where Tab completes a path) is chat_folder()
        got = {}

        class Ctx:
            def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
                got[name] = handler

        island.register(Ctx())
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "gamma")}), \
                mock.patch.object(island, "chat_folder", return_value=self.w("b")), \
                mock.patch("os.getcwd", return_value=str(self.hermes)), mock.patch.object(island, "run_hermes", self.run_):
            self.assertIn("has its own profile: `beta`", got["island"]("", platform="local"))
            self.assertIn("has its own profile: `beta`", got["island"]("src", platform="local"))  # relative: in ~/work/b
            self.assertIn("hermes -p named", got["island"]("../named", platform="local"))
        self.assertEqual(self.run_.calls[0][0][:3], ["profile", "create", "named"])

    def test_the_bare_command_asks_in_a_modal_picker_and_only_a_yes_acts(self):
        gamma = self.hermes / "profiles" / "gamma"
        asked = []
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(gamma), "TERMINAL_CWD": self.w("named")}), \
                mock.patch.object(island, "run_hermes", self.run_):
            for answer in ("no", None):  # no, or a cancelled picker
                self.assertIn("Nothing changed", island.command("", ask=lambda q, c: asked.append(q) or answer))
            self.assertEqual(self.run_.calls, [])
            self.assertIn("own profile `named`", asked[0])
            self.assertEqual(len(asked), 2)
            left = []
            out = island.command("", ask=lambda q, c: "yes", leave=lambda: left.append(1) or True)
            self.assertIn("hermes -p named", out)
            self.assertIn("Leaving this chat", out)
            self.assertEqual(left, [1])  # only the yes leaves
            self.assertTrue(self.run_.calls)  # only the yes acted
            island.command(self.w("a"), ask=lambda q, c: asked.append(q))  # a PATH does not ask
            self.assertEqual(len(asked), 2)

    def test_no_path_in_a_home_folder_chat_uses_the_codebase_of_the_chat_own_calls(self):
        gamma = self.hermes / "profiles" / "gamma"
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(gamma), "TERMINAL_CWD": str(self.home)}), \
                mock.patch("os.getcwd", return_value=str(self.home)), mock.patch.object(island, "run_hermes", self.run_):
            self.assertIn("is in no codebase", island.command("", session_id="s1"))  # no calls yet
            island.save(str(gamma), {"other": {"roots": {self.w("b"): 9}, "t": 2},
                                     "s1": {"roots": {self.w("a"): 2, self.w("named"): 5}, "t": 1}})
            self.assertIn("hermes -p named", island.command("", session_id="s1"))  # this chat, not the newest chat


def tool_db(path: Path, calls) -> Path:
    """A state.db with Hermes's messages columns that islands read: (session id, tool name, arguments) rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                "tool_calls TEXT, timestamp REAL)")
    for i, (sid, name, args) in enumerate(calls):
        call = [{"id": "c%d" % i, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]
        con.execute("INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, 'assistant', ?, ?)",
                    (sid, json.dumps(call), 1000.0 + i))
    con.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s9', 'user', 'hi', 1.0)")
    con.commit()
    con.close()
    return path


class OfferHookTest(Sandbox):
    """The automatic offer: post_tool_call finds the codebases of a chat, transform_llm_output adds the offer."""

    def tracker(self, profile="gamma"):
        return island.Tracker(home=str(self.home), hermes=str(self.hermes), profile_home=island.profile_home(profile, str(self.hermes)))

    def calls(self, t, root, n, sid="s1", start=0):
        for i in range(start, start + n):
            t.post_tool_call(tool_name="read_file", args={"path": "%s/f%d.py" % (root, i)}, session_id=sid)

    def test_the_codebases_that_a_tool_call_names(self):
        a, b = self.w("a"), self.w("b")
        roots = lambda args: island.roots_of(args, str(self.home), str(self.hermes))  # noqa: E731
        self.assertEqual(roots({"path": a + "/README.md"}), {a})
        self.assertEqual(roots({"command": "cd %s && cat ~/work/b/src/x.py" % a}), {a, b})
        self.assertEqual(roots({"command": "curl -s https://example.org/work/a/x"}), set())
        self.assertEqual(roots({"code": "open('~/work/b/data.txt')"}), {b})
        self.assertEqual(roots({"paths": [a + "/x", str(self.hermes / "config.yaml")]}), {a})
        self.assertEqual(roots(None), set())

    def test_a_profile_with_no_island_is_told_at_its_first_codebase(self):
        t = self.tracker()  # gamma: no island (its terminal.cwd is in no codebase)
        self.assertIsNone(t.transform_llm_output(response_text="Done.", session_id="s1"))  # no codebase yet
        self.calls(t, self.w("named"), 1)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")  # the first call is enough
        self.assertTrue(out.startswith("Done.\n\n> [!CAUTION]\n> \U0001F534 **breadcrumbs: this profile (`gamma`) has no "
                                       "island, and this chat works in `~/work/named` (1 call)"), out)
        self.assertIn("`/island ~/work/named`", out)
        self.assertNotIn("mixes", out)
        self.calls(t, self.w("named"), 5, start=30)
        self.assertIsNone(t.transform_llm_output(response_text="Next.", session_id="s1"))  # once per codebase

    def test_a_profile_with_no_island_is_told_at_its_first_reply_in_the_launch_codebase(self):
        t = self.tracker()  # gamma: no island
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("named") + "/src"}):
            out = t.transform_llm_output(response_text="Ready.", session_id="s1")  # no tool call yet
            self.assertTrue(out.startswith("Ready.\n\n> [!CAUTION]\n> \U0001F534 **breadcrumbs: this profile (`gamma`) has no "
                                           "island, and this chat starts in `~/work/named` (the launch folder)"), out)
            self.assertIn("`/island ~/work/named`", out)
            self.assertIsNone(t.transform_llm_output(response_text="Next.", session_id="s1"))  # once per codebase
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": str(self.home)}):  # no codebase there
            self.assertIsNone(t.transform_llm_output(response_text="Ready.", session_id="s2"))
            with mock.patch("os.getcwd", return_value=self.w("named")):  # the process folder is a codebase
                self.assertIn("starts in `~/work/named`", t.transform_llm_output(response_text="Ready.", session_id="s3"))

    def test_the_folder_that_hermes_pinned_for_the_chat_is_its_launch_folder(self):
        import sys
        import types
        rc = types.ModuleType("agent.runtime_cwd")
        pinned = {"cwd": self.w("named") + "/src"}
        rc.resolve_agent_cwd = lambda: Path(pinned["cwd"])
        t = self.tracker()  # gamma: no island
        with mock.patch.object(island, "chat_folder", self._real_chat_folder), \
                mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.runtime_cwd": rc}), \
                mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("a")}), mock.patch("os.getcwd", return_value=self.w("b")):
            self.assertEqual(island.chat_folder(), self.w("named") + "/src")  # not TERMINAL_CWD, not the process folder
            self.assertIn("starts in `~/work/named`", t.transform_llm_output(response_text="Ready.", session_id="s1"))
            pinned["cwd"] = str(self.home)  # a chat in the home folder: no codebase, whatever the process env says
            self.assertIsNone(t.transform_llm_output(response_text="Ready.", session_id="s2"))

    def test_the_startup_hook_keeps_the_launch_codebase_for_the_first_reply(self):
        t = self.tracker()  # gamma: no island
        gamma = island.profile_home("gamma", str(self.hermes))
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": str(self.home)}), mock.patch("os.getcwd", return_value=self.w("named")):
            t.on_session_start(session_id="s1", model="m", platform="cli")
        self.assertEqual(island.load(gamma)["s1"]["launch"], self.w("named"))
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": str(self.home)}), mock.patch("os.getcwd", return_value=str(self.home)):
            out = t.transform_llm_output(response_text="Ready.", session_id="s1")  # the kept launch, not the folder now
        self.assertIn("starts in `~/work/named` (the launch folder)", out)
        self.assertEqual(island.load(gamma)["s1"]["warned"], [self.w("named")])
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": str(self.home)}), mock.patch("os.getcwd", return_value=str(self.home)):
            t.on_session_start(session_id="s2")  # no codebase: nothing kept
        self.assertNotIn("s2", island.load(gamma))

    def test_an_island_profile_is_told_at_its_first_reply_when_the_chat_starts_in_another_codebase(self):
        t = self.tracker("alpha")  # the island of ~/work/a
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("named")}):
            out = t.transform_llm_output(response_text="Ready.", session_id="s1")
            self.assertIn("this profile is the island of `~/work/a`, and this chat starts in `~/work/named` (the launch folder)", out)
            self.assertIsNone(t.transform_llm_output(response_text="Next.", session_id="s1"))  # once
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("a")}):  # its own codebase: no warning, but the way to the visualizer
            out = t.transform_llm_output(response_text="Ready.", session_id="s2")
            self.assertNotIn("CAUTION", out)
            self.assertIn("[!NOTE]", out)
            self.assertIn("starts in the island `~/work/a` (profile `alpha`)", out)
            self.assertIn("alpha/breadcrumbs term`", out)
            self.assertIn("alpha/breadcrumbs serve --open`", out)
            self.assertIn("In this chat: `/viz`", out)
            self.assertIsNone(t.transform_llm_output(response_text="Next.", session_id="s2"))  # once per chat

    def test_a_source_tree_with_no_git_repository_is_a_codebase_and_git_wins(self):
        write(self.home / "work" / "tree" / "package.json", "{}")
        write(self.home / "work" / "tree" / "src" / "deep" / "x.js")
        write(self.home / "work" / "a" / "pkg" / "package.json", "{}")  # a package inside the repository ~/work/a
        root = lambda p: island.root_of(p, str(self.home), str(self.hermes))  # noqa: E731
        self.assertEqual(root(self.w("tree/src/deep/x.js")), self.w("tree"))
        self.assertEqual(root(self.w("a/pkg/x.py")), self.w("a"))  # the repository, not the package
        self.assertIsNone(root(self.w("plain")))
        t = self.tracker()
        with mock.patch.dict(os.environ, {"TERMINAL_CWD": self.w("tree/src")}):
            self.assertIn("starts in `~/work/tree` (the launch folder)", t.transform_llm_output(response_text="Ready.", session_id="s1"))

    def test_the_default_profile_is_told_too(self):
        write(self.hermes / "config.yaml", "terminal:\n  cwd: .\n")  # the global profile: no island
        t = island.Tracker(home=str(self.home), hermes=str(self.hermes), profile_home=str(self.hermes))
        self.calls(t, self.w("named"), island.MIN_CALLS)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("this profile (`default`) has no island", out)
        self.assertIn("`/island ~/work/named`", out)

    def test_a_chat_that_reaches_a_second_codebase_gets_the_offer_once(self):
        t = self.tracker()
        self.calls(t, self.w("named"), 4)
        self.assertIn("has no island", t.transform_llm_output(response_text="Done.", session_id="s1"))
        self.calls(t, self.w("c"), island.MIN_CALLS, start=10)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertTrue(out.startswith("Done.\n\n> [!CAUTION]\n> \U0001F534 **breadcrumbs: this chat mixes two codebases"), out)
        self.assertIn("`~/work/named` (4 calls) and `~/work/c` (%d calls)" % island.MIN_CALLS, out)
        self.assertIn("`/island ~/work/c`", out)  # the command, not an action: /island changes nothing by itself
        self.assertNotIn("`/island ~/work/named`", out)  # that codebase had its offer
        self.assertIn(island.WHY, out)
        self.assertNotIn("spacegraph", out.lower())
        self.assertNotIn("plugins install", out)
        self.calls(t, self.w("c"), 5, start=30)
        self.assertIsNone(t.transform_llm_output(response_text="Next.", session_id="s1"))  # once per codebase
        self.assertIsNone(t.transform_llm_output(response_text="Other chat.", session_id="s2"))  # no calls in s2

    def test_a_codebase_with_an_island_points_at_its_profile(self):
        t = self.tracker()
        self.calls(t, self.w("b"), 3)
        self.calls(t, self.w("a"), 3, start=10)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("`~/work/a` has its own profile: continue that work there: `hermes -p alpha`", out)

    def test_an_island_profile_is_told_at_its_first_foreign_codebase(self):
        t = self.tracker("alpha")  # the island of ~/work/a
        self.calls(t, self.w("a"), 1)
        self.calls(t, self.w("named"), 3, start=10)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("this profile is the island of `~/work/a`, and this chat works in `~/work/named` (3 calls)", out)

    def test_the_counts_stay_in_their_profile_and_chat(self):
        g, b = self.tracker("gamma"), self.tracker("src-c")  # two profiles with no island
        self.calls(g, self.w("a"), 3)
        self.calls(b, self.w("named"), 3)
        self.calls(g, self.w("named"), 3, sid="s2")
        out = g.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("works in `~/work/a`", out)
        self.assertNotIn("named", out)
        out = b.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("works in `~/work/named`", out)
        self.assertNotIn("~/work/a", out)
        out = g.transform_llm_output(response_text="Done.", session_id="s2")
        self.assertIn("works in `~/work/named`", out)
        self.assertNotIn("~/work/a", out)

    def test_hermes_own_files_and_tools_never_count(self):
        t = self.tracker()
        self.calls(t, self.w("a"), 3)
        self.calls(t, str(self.hermes / "skills"), 5, start=10)
        self.calls(t, str(self.home / ".pyenv" / "repo"), 5, start=20)
        out = t.transform_llm_output(response_text="Done.", session_id="s1")
        self.assertIn("works in `~/work/a` (3 calls)", out)  # a only: no skills folder, no tool repository
        self.assertEqual(island.load(island.profile_home("gamma", str(self.hermes)))["s1"]["roots"], {self.w("a"): 3})

    def test_a_bad_reply_or_bad_arguments_change_nothing(self):
        t = self.tracker()
        t.post_tool_call(tool_name="x", args="not a dict", session_id="s1")
        self.assertIsNone(t.transform_llm_output(response_text="", session_id="s1"))
        self.assertIsNone(t.transform_llm_output(response_text=None, session_id="s1"))
        self.assertFalse(os.path.exists(island.state_path(island.profile_home("gamma", str(self.hermes)))))

    def test_the_counts_live_in_a_file_and_survive_a_reload_of_the_plugin(self):
        # The dev shim purges breadcrumbs' modules at each agent build: a count in memory would start again.
        self.calls(self.tracker(), self.w("a"), 4)
        self.calls(self.tracker(), self.w("named"), 3, start=10)
        gamma = island.profile_home("gamma", str(self.hermes))
        path = island.state_path(gamma)
        self.assertEqual(path, os.path.join(gamma, "breadcrumbs", "islands.json"))
        chat = island.load(gamma)["s1"]
        self.assertEqual(chat["roots"], {self.w("a"): 4, self.w("named"): 3})
        self.assertIn("[!CAUTION]", self.tracker().transform_llm_output(response_text="Done.", session_id="s1"))
        self.assertEqual(island.load(gamma)["s1"]["warned"], [self.w("a"), self.w("named")])  # the signal stays in islands.json
        self.assertIsNone(self.tracker().transform_llm_output(response_text="Next.", session_id="s1"))
        self.assertEqual([n for n in os.listdir(os.path.dirname(path))], ["islands.json"])  # no temp file left

    def test_the_file_keeps_the_newest_chats(self):
        gamma = island.profile_home("gamma", str(self.hermes))
        island.save(gamma, {"s%03d" % i: {"roots": {}, "t": i} for i in range(island.KEEP + 5)})
        kept = island.load(gamma)
        self.assertEqual(len(kept), island.KEEP)
        self.assertNotIn("s004", kept)
        self.assertIn("s005", kept)
        write(Path(island.state_path(gamma)), "not json")
        self.assertEqual(island.load(gamma), {})

    def test_the_profile_of_the_hook_is_the_running_profile(self):
        t = island.Tracker()  # as register() makes it: the profile comes from Hermes at each call
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "gamma")}):
            self.calls(t, self.w("a"), 3)
            self.calls(t, self.w("named"), 3, start=10)
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "src-c")}):
            self.assertIsNone(t.transform_llm_output(response_text="Done.", session_id="s1"))
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(self.hermes / "profiles" / "gamma")}):
            self.assertIn("`/island ~/work/named`", t.transform_llm_output(response_text="Done.", session_id="s1"))


class CheckTest(Sandbox):
    """Verified islands: the checks over all profiles, and the codebases of a profile's own tool calls."""

    def test_status_reports_a_shared_and_a_missing_island(self):
        write(self.hermes / "profiles" / "lost" / "config.yaml", "terminal:\n  cwd: %s\n" % self.w("gone"))
        lines = island.checks(str(self.home), str(self.hermes))
        text = "\n".join(lines)
        self.assertIn("`dup`: its terminal.cwd is in `~/work/a`, the island of `alpha`", text)
        self.assertIn("`lost`: its terminal.cwd `%s` does not exist" % self.w("gone"), text)
        self.assertEqual(len(lines), 2, text)
        self.assertIn("Checks: 2 problems", island.status("gamma", str(self.home), str(self.hermes)).text)

    def test_sound_islands_pass(self):
        shutil.rmtree(self.hermes / "profiles" / "dup")
        self.assertEqual(island.checks(str(self.home), str(self.hermes)), [])
        self.assertIn("Checks: every island is sound", island.status("gamma", str(self.home), str(self.hermes)).text)

    def test_the_codebases_of_a_profiles_tool_calls(self):
        a, n = self.w("a"), self.w("named")
        tool_db(self.hermes / "profiles" / "gamma" / "state.db",
                [("s1", "read_file", {"path": a + "/x.py"})] * 4 + [("s2", "terminal", {"command": "ls %s" % n})] * 2
                + [("s2", "read_file", {"path": a + "/y.py"}), ("s3", "terminal", {"command": "ls /tmp"})])
        got = island.scan(str(self.hermes / "profiles" / "gamma"), str(self.home), str(self.hermes))
        self.assertEqual({r: v[:2] for r, v in got.items()}, {a: [5, 2], n: [2, 1]})
        text = island.status("gamma", str(self.home), str(self.hermes)).text
        self.assertIn("  ~/work/a: 5 calls in 2 chats (the island of `alpha`: `hermes -p alpha`)", text)
        self.assertIn("  ~/work/named: 2 calls in 1 chat (no island: `/island ~/work/named`)", text)
        self.assertEqual(island.scan(str(self.hermes / "profiles" / "beta"), str(self.home), str(self.hermes)), {})

    def test_the_report_of_all_profiles(self):
        a, b = self.w("a"), self.w("b")
        tool_db(self.hermes / "profiles" / "alpha" / "state.db", [("s1", "read_file", {"path": a + "/x"})] * 9
                + [("s1", "read_file", {"path": b + "/y"})])
        tool_db(self.hermes / "profiles" / "src-c" / "state.db", [("s1", "read_file", {"path": self.w("c") + "/x"})] * 3)
        text = island.report(str(self.home), str(self.hermes)).text
        self.assertTrue(text.startswith("breadcrumbs islands of all profiles"), text)
        self.assertIn("  alpha: island `~/work/a`; its calls: ~/work/a 90%, ~/work/b 10%: mixes 2 codebases", text)
        self.assertIn("  beta: island `~/work/b`; no codebase in its calls", text)
        self.assertIn("  src-c: no island; its calls: ~/work/c 100%: `/island ~/work/c src-c`", text)


class PathTextTest(unittest.TestCase):
    """The text forms of a path on Windows and on POSIX. These functions read text only, so each platform
    runs both forms."""

    def form(self, windows):
        p = mock.patch.object(island, "WINDOWS", windows)
        p.start()
        self.addCleanup(p.stop)

    def test_what_is_a_path(self):
        cases = {  # text: (on POSIX, on Windows)
            "/x": (True, False), "~/x": (True, True), "C:\\x": (False, True), "c:/x": (False, True),
            "\\\\host\\share\\x": (False, True), "~\\x": (False, True), "C:x": (False, False),
            ".": (False, False), "x/y": (False, False), "": (False, False),
        }
        for windows in (False, True):
            with mock.patch.object(island, "WINDOWS", windows):
                for text, want in cases.items():
                    self.assertIs(island.is_path(text), want[windows], (text, windows))

    def test_the_slash_command_keeps_the_backslashes_of_a_windows_path(self):
        self.form(True)
        self.assertEqual(island.words(r"C:\Users\me\work\a named"), [r"C:\Users\me\work\a", "named"])
        self.assertEqual(island.words(r'"C:\Users\me\two words\a" named'), [r"C:\Users\me\two words\a", "named"])
        self.assertEqual(island.words(r"'C:\x y'"), [r"C:\x y"])
        self.assertEqual(island.words("~/work/a  n"), ["~/work/a", "n"])
        self.assertEqual(island.words(" --all "), ["--all"])
        self.assertEqual(island.words(""), [])
        self.assertEqual(island.words(r'"C:\x y'), [r'"C:\x', "y"])  # no closing quote: the words between the spaces

    def test_on_posix_a_backslash_in_the_slash_command_is_the_escape_character(self):
        self.form(False)
        self.assertEqual(island.words(r"'/x y' /a\ b n"), ["/x y", "/a b", "n"])
        self.assertEqual(island.words("'/x y"), ["'/x", "y"])  # no closing quote
        self.assertEqual(island.words(""), [])

    def test_the_windows_paths_that_a_tool_call_names(self):
        self.form(True)
        a = r"C:\Users\me\work\a"
        self.assertEqual(island.paths_in({"path": a + r"\README.md"}), [a + r"\README.md"])
        self.assertEqual(island.paths_in({"command": r"cd %s && type ~\work\b\x.py C:/Users/me/work/c/y.py ~/work/d" % a}),
                         [a, r"~\work\b\x.py", "C:/Users/me/work/c/y.py", "~/work/d"])
        self.assertEqual(island.paths_in({"code": r"open('C:\\Users\\me\\work\\b\\data.txt')"}),
                         [r"C:\\Users\\me\\work\\b\\data.txt"])  # code doubles each backslash: one separator
        self.assertEqual(island.paths_in({"command": "curl -s https://example.org/work/a/x"}), [])
        self.assertEqual(island.paths_in({"paths": [a + "/x", r"rel\x", "C:x", r"\\host\share\work\a"]}),
                         [a + "/x", r"\\host\share\work\a"])

    def test_the_posix_paths_that_a_tool_call_names(self):
        self.form(False)
        self.assertEqual(island.paths_in({"command": "cd /srv/work/a && cat ~/work/b/x.py", "path": "/srv/x", "file": "rel/x"}),
                         ["/srv/work/a", "~/work/b/x.py", "/srv/x"])
        self.assertEqual(island.paths_in({"command": "curl -s https://example.org/work/a/x"}), [])


class SourcesTest(Sandbox):
    """`python -m breadcrumbs sources PROFILE`: the codebases of a profile's tool calls, from its state.db."""

    def state_db(self, home):
        """A state.db with tool calls in two codebases (s1, s2) and two rows that hold no call."""
        def call(args):
            return json.dumps([{"function": {"name": "terminal", "arguments": json.dumps(args)}}])

        a, b = self.w("a"), self.w("b")
        con = sqlite3.connect(os.path.join(home, "state.db"))
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL, tool_calls TEXT)")
        rows = [("s1", 100, call({"path": a + "/x"})), ("s1", 200, call({"command": "ls %s" % a})),
                ("s2", 300, call({"path": a + "/y"})), ("s2", 400, call({"path": b + "/z"})),
                ("s2", 500, None), ("s3", 600, "not json")]
        con.executemany("INSERT INTO messages (session_id, timestamp, tool_calls) VALUES (?, ?, ?)", rows)
        con.commit()
        con.close()

    def test_scan_counts_the_codebases_of_a_profile_from_its_state_db(self):
        home = str(self.hermes / "profiles" / "beta")
        self.state_db(home)
        self.assertEqual(island.scan(home), {self.w("a"): [3, 2, 300], self.w("b"): [1, 1, 400]})

    def test_the_sources_command_lists_the_codebases_and_marks_the_island_of_the_profile(self):
        home = str(self.hermes / "profiles" / "beta")  # its island is ~/work/b
        self.state_db(home)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["sources", home]), 0)
        self.assertEqual(out.getvalue().splitlines(), [
            "      3 calls     2 chats  %s  ~/work/a" % time.strftime("%Y-%m-%d", time.localtime(300)),
            "      1 calls     1 chats  %s  ~/work/b  (the island of this profile)"
            % time.strftime("%Y-%m-%d", time.localtime(400))])


if __name__ == "__main__":
    unittest.main()
