"""The repo is the plugin: the catalog clones it at a pinned commit and Hermes loads the root."""
import importlib.util
import io
import contextlib
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

from breadcrumbs import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The hooks that register() adds, in order: the three of the islands (island.register), then the one of the dial
# family (hermes.register_handles). plugin.yaml lists the same four in provides_hooks.
HOOKS = ["on_session_start", "post_tool_call", "transform_llm_output", "post_api_request", "post_auxiliary_call",
         "transform_tool_result"]


class Ctx:
    def __init__(self):
        self.providers = []

    def register_memory_provider(self, provider):
        self.providers.append(provider)


class HookCtx(Ctx):
    """A Hermes with plugin hooks (its memory-provider collector forwards register_hook)."""

    def __init__(self):
        super().__init__()
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append(name)


class CommandCtx(HookCtx):
    """A Hermes whose plugin context has slash commands and CLI commands (its memory-provider collector
    forwards register_command to a real PluginContext)."""

    def __init__(self):
        super().__init__()
        self.commands, self.cli = [], []

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands.append(name)

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli.append(name)


class SkillCtx(Ctx):
    """A Hermes with plugin skills, as its memory-provider collector: ctx.name is the plugin's name."""

    def __init__(self, name=None):
        super().__init__()
        self.skills = []
        if name:
            self.name = name

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, str(path), description))


def load_root(parent="bcprobe"):
    """Load ROOT/__init__.py as Hermes's directory loader does: a package under a namespace parent."""
    ns = types.ModuleType(parent)
    ns.__path__ = []
    sys.modules[parent] = ns
    name = parent + ".breadcrumbs"
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "__init__.py"),
                                                  submodule_search_locations=[ROOT])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest():
    out = {}
    with open(os.path.join(ROOT, "plugin.yaml")) as f:
        for line in f:
            key, sep, value = line.partition(":")
            if sep and not line[0].isspace():
                out[key.strip()] = value.strip()
    return out


class RootPluginTest(unittest.TestCase):
    def tearDown(self):
        for k in [k for k in sys.modules if k == "bcprobe" or k.startswith("bcprobe.")]:
            del sys.modules[k]

    def test_the_root_registers_a_new_provider_per_load(self):
        module = load_root()
        a, b = Ctx(), Ctx()
        module.register(a)
        module.register(b)
        self.assertEqual(len(a.providers), 1)
        self.assertEqual(a.providers[0].name, "breadcrumbs")
        self.assertIsNot(a.providers[0], b.providers[0])

    def test_the_root_registers_the_dispatch_handbook_with_the_tools(self):
        ctx = SkillCtx()
        load_root().register(ctx)
        [(name, path, about)] = ctx.skills
        self.assertEqual(name, "dispatch")
        self.assertTrue(os.path.isfile(path), path)
        self.assertTrue(0 < len(about) <= 60, about)
        self.assertEqual(ctx.providers[0].handbook, "breadcrumbs:dispatch")

    def test_the_handbook_takes_the_name_that_hermes_gave_the_plugin(self):
        ctx = SkillCtx(name="bcdev")
        load_root().register(ctx)
        self.assertEqual(ctx.providers[0].handbook, "bcdev:dispatch")

    def test_a_hermes_without_plugin_skills_gets_the_provider_and_no_handbook_line(self):
        ctx = Ctx()
        load_root().register(ctx)
        self.assertEqual(len(ctx.providers), 1)
        self.assertIsNone(ctx.providers[0].handbook)

    def test_a_hermes_whose_register_skill_fails_still_gets_the_provider(self):
        class OtherHermes(Ctx):
            def register_skill(self, name):  # another signature: the call raises TypeError
                raise AssertionError("not reached")
        ctx = OtherHermes()
        load_root().register(ctx)
        self.assertEqual(len(ctx.providers), 1)
        self.assertIsNone(ctx.providers[0].handbook)

    def test_the_root_registers_the_island_hooks_where_hermes_has_hooks(self):
        ctx = HookCtx()
        load_root().register(ctx)
        self.assertEqual(ctx.hooks, HOOKS)

    def test_the_root_registers_the_island_slash_command_and_cli_command(self):
        ctx = CommandCtx()
        load_root().register(ctx)
        self.assertEqual(ctx.commands, ["island", "viz"])  # /island PATH [NAME]: the handoff, and the checks; /viz: the explorer
        self.assertEqual(ctx.cli, ["islands"])  # hermes islands island PATH [NAME] | status [--all]
        self.assertEqual(ctx.hooks, HOOKS)

    def test_the_manifest_declares_every_hook_that_the_root_registers(self):
        # `hermes plugins validate` (the catalog gate) fails on a hook that register() adds and plugin.yaml
        # does not list in provides_hooks (hermes_cli/plugin_validate.py, _check_capabilities).
        ctx = HookCtx()
        load_root().register(ctx)
        declared = [h.strip() for h in manifest().get("provides_hooks", "").strip("[]").split(",") if h.strip()]
        self.assertEqual(sorted(declared), sorted(set(ctx.hooks)))

    def test_no_file_that_can_run_calls_sudo(self):
        # Hermes scans each plugin install and update (tools/plugin_guard.py). `sudo` in a file that can run
        # is high (sudo_usage), and high gives "caution": `plugins update` stops and asks, and a run with no
        # terminal stops with "BLOCKED". Root steps for a host go in a docs runbook, which a person types.
        hits = []
        for top, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", ".notes", ".ignore", "node_modules", "var", "__pycache__", "data", "tests")]
            for name in files:
                if name.endswith(".md"):
                    continue
                path = os.path.join(top, name)
                with open(path, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
                hits += ["%s:%d" % (os.path.relpath(path, ROOT), i) for i, line in enumerate(text.splitlines(), 1)
                         if re.search(r"\bsudo\b(?!\.\w)", line)]
        self.assertEqual(hits, [])

    def test_the_manifest_and_pyproject_give_one_version(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            py = [line.split("=", 1)[1].strip() for line in f if line.startswith("version =")]
        self.assertEqual(py, [manifest()["version"]])

    def test_hermes_finds_the_root_as_a_memory_provider_and_a_context_engine(self):
        # Hermes finds a user plugin's memory provider and context engine by these texts in its __init__.py
        # (plugins/memory: _is_memory_provider_dir; plugins/context_engine: _is_context_engine_dir). Without
        # them, memory.provider and context.engine breadcrumbs load nothing.
        with open(os.path.join(ROOT, "__init__.py"), encoding="utf-8") as f:
            src = f.read()[:8192]
        self.assertIn("register_memory_provider", src)
        self.assertIn("register_context_engine", src)

    def test_the_manifest_names_the_plugin_and_a_hermes_floor(self):
        m = manifest()
        self.assertEqual(m["name"], "breadcrumbs")
        self.assertRegex(m["version"], r'^"\d+\.\d+\.\d+"$')
        self.assertRegex(m["requires_hermes"], r'^">=\d+\.\d+(\.\d+)?"$')
        self.assertTrue(m["description"])

    def test_the_manifest_names_the_author_the_homepage_and_the_tags(self):
        m = manifest()
        self.assertEqual(m["author"], '"breadcrumbs-agency"')
        self.assertEqual(m["homepage"], "https://github.com/breadcrumbs-agency/hermes-breadcrumb-plugin")
        self.assertEqual([t.strip() for t in m["tags"].strip("[]").split(",")],
                         ["memory", "context-engine", "provenance", "ipfs"])
        self.assertEqual(m["license"], "MIT")

    def test_hermes_installs_one_package_for_the_plugin_and_does_not_build_it(self):
        """Hermes's installer puts the dependencies of a [project] table into its shared environment
        (it asks for consent first). The plugin declares one: google-crc32c, on CPython only, so GraalPy
        builds nothing. With a build system, extras, dependency groups or a uv package flag, Hermes
        would also build the plugin itself into the environment: it declares none of them."""
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
            doc = tomllib.load(f)
        project, uv = doc.get("project", {}), doc.get("tool", {}).get("uv", {})
        self.assertEqual(project.get("dependencies"),
                         ["google-crc32c>=1.5,<2; platform_python_implementation == 'CPython'"])
        self.assertNotIn("build-system", doc)
        self.assertFalse(any(project.get("optional-dependencies", {}).values()))
        self.assertFalse(any(doc.get("dependency-groups", {}).values()))
        self.assertIsNot(uv.get("package"), True)
        self.assertFalse(uv.get("dev-dependencies"))


class InstallShimTest(unittest.TestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def test_install_hermes_writes_the_shim(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p"), 0)
            with open(os.path.join(home, "plugins", "breadcrumbs", "__init__.py")) as f:
                self.assertIn("plugin shim, written by", f.read())

    def test_the_shim_registers_the_provider_and_the_handbook_through_the_repo(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p"), 0)
            path = os.path.join(home, "plugins", "breadcrumbs", "__init__.py")
            with open(path) as f:
                src = f.read()
        self.assertIn("BreadcrumbsProvider", src)  # seats.py: a shim that names it is breadcrumbs, not a seat
        ns: dict = {"__name__": "bcshim"}
        exec(compile(src, path, "exec"), ns)
        ctx = SkillCtx()
        ns["register"](ctx)
        self.assertEqual([s[0] for s in ctx.skills], ["dispatch"])
        self.assertEqual(ctx.providers[0].handbook, "breadcrumbs:dispatch")

    def test_the_shim_registers_ace_under_its_own_name_where_the_profile_selects_it(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p", "--name", "bcdev2"), 0)
            path = os.path.join(home, "plugins", "bcdev2", "__init__.py")
            with open(path) as f:
                src = f.read()
            with open(os.path.join(home, "config.yaml"), "w") as f:
                f.write("context:\n  engine: bcdev2\n")
            engines = []

            class EngineCtx(Ctx):
                def register_context_engine(self, engine):
                    engines.append(engine)

            ns: dict = {"__name__": "bcshim2"}
            exec(compile(src, path, "exec"), ns)
            with mock.patch.dict(os.environ, {"HERMES_HOME": home}):
                ns["register"](EngineCtx())
        self.assertEqual([e.name for e in engines], ["bcdev2"])

    def test_hermes_finds_the_shim_as_a_memory_provider_and_a_context_engine(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p"), 0)
            with open(os.path.join(home, "plugins", "breadcrumbs", "__init__.py"), encoding="utf-8") as f:
                src = f.read()[:8192]
        self.assertIn("register_memory_provider", src)  # as for the root, above
        self.assertIn("register_context_engine", src)

    def test_the_shim_registers_the_island_hooks(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p"), 0)
            path = os.path.join(home, "plugins", "breadcrumbs", "__init__.py")
            with open(path) as f:
                src = f.read()
        ns: dict = {"__name__": "bcshim3"}
        exec(compile(src, path, "exec"), ns)
        ctx = HookCtx()
        ns["register"](ctx)
        self.assertEqual(ctx.hooks, HOOKS)

    def test_the_shim_registers_the_island_command(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            self.assertEqual(self.run_cli("install-hermes", "p"), 0)
            path = os.path.join(home, "plugins", "breadcrumbs", "__init__.py")
            with open(path) as f:
                src = f.read()
        ns: dict = {"__name__": "bcshim4"}
        exec(compile(src, path, "exec"), ns)
        ctx = CommandCtx()
        ns["register"](ctx)
        self.assertEqual(ctx.commands, ["island", "viz"])
        self.assertEqual(ctx.cli, ["islands"])

    def test_install_hermes_asks_for_no_other_plugin(self):
        # breadcrumbs alone gives /island, the handoff, the checks and the offer: no second plugin to install.
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["install-hermes", "p"]), 0)
        text = out.getvalue()
        self.assertNotIn("protect:", text)
        self.assertNotIn("spacegraph", text.lower())

    def test_a_shim_for_a_windows_path_compiles_and_keeps_the_path(self):
        repo = "C:\\Users\\someone\\work\\breadcrumbs"  # "\\U" would start an escape in a plain string
        src = cli.SHIM.format(repo=repo, name="bc", dev=True)
        ns = {}
        exec(compile(src, "shim.py", "exec"), ns)
        self.assertEqual(ns["_REPO"], repo)
        self.assertIn(repo, ns["__doc__"])

    def test_install_hermes_never_overwrites_a_plugin_it_did_not_write(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "profile_home", return_value=home):
            target = os.path.join(home, "plugins", "breadcrumbs")
            os.makedirs(target)
            with open(os.path.join(target, "__init__.py"), "w") as f:
                f.write("# installed from the catalog\n")
            self.assertNotEqual(self.run_cli("install-hermes", "p"), 0)
            with open(os.path.join(target, "__init__.py")) as f:
                self.assertEqual(f.read(), "# installed from the catalog\n")
            self.assertFalse(os.path.exists(os.path.join(target, "plugin.yaml")))


class HandbookTest(unittest.TestCase):
    """The delegation and dispatch handbook goes with the tool descriptions as the plugin skill dispatch."""

    def setUp(self):
        from breadcrumbs import hermes
        self.hermes = hermes
        with open(hermes.HANDBOOK, encoding="utf-8") as f:
            self.text = f.read()

    def test_it_is_five_lines_and_no_more(self):
        """The user's rule. Hermes takes the skill's name and description from register_skill, so the
        file has no front matter."""
        lines = self.text.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(line.strip() for line in lines))

    def test_it_teaches_the_least_manufactured_intent_with_exact_pieces(self):
        for words in ("least manufactured intent", "chess pieces", "level 1", "id:a-b", "crumb_find",
                      "delegate_task", "message_agent", "cron"):
            self.assertIn(words, self.text)

    def test_its_bc_command_starts_in_the_folder_that_holds_the_package(self):
        self.assertIn('cd "${HERMES_SKILL_DIR}/../../.." && python3 -m breadcrumbs bc', self.text)
        repo = os.path.join(os.path.dirname(self.hermes.HANDBOOK), "..", "..", "..")
        self.assertTrue(os.path.isfile(os.path.join(repo, "breadcrumbs", "cli.py")))

    def test_it_has_no_words_that_hermes_flags_as_prompt_injection(self):
        for p in ("ignore previous instructions", "ignore all previous", "you are now", "disregard your",
                  "forget your instructions", "new instructions:", "system prompt:", "<system>", "]]>"):
            self.assertNotIn(p, self.text.lower())


if __name__ == "__main__":
    unittest.main()
