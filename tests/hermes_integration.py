"""Integration check through Hermes's own MemoryManager (run with the Hermes venv python).

  HERMES_PY=<the python of the Hermes venv, e.g. $(dirname $(readlink -f $(which hermes)))/python>
  $HERMES_PY tests/hermes_integration.py

It uses a scratch HERMES_HOME. It never touches a real profile.
"""

import json
import os
import sys
import tempfile

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # no real model here: unit tests cover it

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from agent.memory_manager import MemoryManager  # noqa: E402

from breadcrumbs.hermes import BreadcrumbsProvider  # noqa: E402


def main():
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        mgr = MemoryManager()
        mgr.add_provider(BreadcrumbsProvider())
        mgr.initialize_all(session_id="it-1", platform="cli", hermes_home=home, agent_context="primary")
        tools = sorted(t["name"] for t in mgr.get_all_tool_schemas())
        print("tools:", tools)
        assert [t.removeprefix("mcp__") for t in tools] == ["crumb", "crumb_find"], tools
        assert "Breadcrumbs memory" in mgr.build_system_prompt()
        T = tools[0][: -len("crumb")]  # the wire prefix in use ("" or "mcp__")

        mgr.sync_all("the load tests run on the build host, never on the laptop at the same time",
                     "noted: build host for load tests", session_id="it-1")
        assert mgr.flush_pending(timeout=10), "sync did not drain"
        found = json.loads(mgr.handle_tool_call(T + "crumb_find", {"q": "build host load tests"}))
        print("crumb_find:\n" + found["text"])
        assert "build host" in found["text"]
        rows = found["text"].splitlines()
        turn = next(rows[i - 1].split()[1] for i, r in enumerate(rows) if "never on the laptop" in r)
        shown = json.loads(mgr.handle_tool_call(T + "crumb", {"id": turn, "level": 1.0}))["text"]
        assert "never on the laptop at the same time" in shown

        mgr.on_session_switch("it-2", reset=True)
        recall = mgr.prefetch_all("where do the load tests run?", session_id="it-2")
        print("prefetch (%d chars):\n%s" % (len(recall), recall))
        assert "build host" in recall
        print("recall status:", mgr.describe_recall())

        mgr.on_memory_write("add", "user", "The user wants every reply in Simplified Technical English")
        hit = json.loads(mgr.handle_tool_call(T + "crumb_find", {"q": "technical english"}))["text"]
        assert "Simplified Technical English" in hit
        # A replace through Hermes's own mirror: the store result names the replaced entry (previous_content),
        # so breadcrumbs tags the old mem crumb as superseded by the new one, and recall shows the old one as a handle.
        old, new = "The user wants every reply in Simplified Technical English", "The user wants every reply in plain Dutch"
        mgr.notify_memory_tool_write(json.dumps({"success": True, "replaced_entry": old}),
                                     {"action": "replace", "target": "user", "old_text": "Technical", "content": new})
        mgr.on_session_switch("it-3", reset=True)
        recall = mgr.prefetch_all("which language does the user want in every reply?", session_id="it-3")
        print("prefetch after a replace:\n" + recall)
        assert new in recall and old not in recall and "superseded by" in recall, recall
        mgr.on_session_end([])
        mgr.shutdown_all()
        print("OK: MemoryManager drove every hook; store at", os.path.join(home, "breadcrumbs"))


# A tiny memory provider for the slot check: Hermes's own loader finds it in <HERMES_HOME>/plugins/kettle/.
SLOT_PLUGIN = '''"""A tiny memory provider (breadcrumbs slot check)."""
import json
import os

from agent.memory_provider import MemoryProvider


class KettleMemory(MemoryProvider):
    @property
    def name(self):
        return "kettle"

    def is_available(self):
        return bool(os.environ.get("KETTLE_API_KEY"))

    def unavailable_reason(self):
        return "KETTLE_API_KEY is not set"

    def get_config_schema(self):
        return [{"key": "api_key", "description": "Kettle API key", "secret": True, "required": True,
                 "env_var": "KETTLE_API_KEY"},
                {"key": "shelf", "description": "The kettle shelf", "default": "top", "choices": ["top", "low"]},
                {"key": "lid", "description": "The lid colour", "default": "red", "when": {"shelf": "low"}}]

    def save_config(self, values, hermes_home):
        with open(os.path.join(hermes_home, "kettle.json"), "w") as f:  # where Hermes reads kettle's values
            json.dump(values, f)

    def initialize(self, session_id, **kwargs):
        self.home = str(kwargs.get("hermes_home") or os.environ["HERMES_HOME"])

    def system_prompt_block(self):
        return "## Kettle memory\\nThe kettle memory is on."

    def prefetch(self, query, *, session_id=""):
        return "- the walrus kettle is on the top shelf (kettle memory)"

    def queue_prefetch(self, query, *, session_id=""):
        pass

    def sync_turn(self, user_content, assistant_content, *, session_id="", **kwargs):
        with open(os.path.join(self.home, "kettle-sync.txt"), "a") as f:
            f.write(user_content + "\\n")

    def get_tool_schemas(self):
        return [{"name": "kettle_search", "description": "search the kettle memory",
                 "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        return json.dumps({"result": "kettle hit for " + args.get("q", "")})

    def shutdown(self):
        pass
'''


def slot_setup_check(home):
    """The 3 setup hooks THRU breadcrumbs, by Hermes's own code: the agent build (a slot without its key), and
    the dashboard's read and write of the setup fields (the same fields that `hermes memory setup` asks)."""
    from hermes_cli.config import load_env
    from hermes_cli.web_server_memory import _memory_provider_is_configured
    from hermes_cli.web_routers.memory_providers import _memory_provider_payload, _write_memory_provider_config_values
    mgr, bc = MemoryManager(), BreadcrumbsProvider()  # 1. kettle lacks its key: breadcrumbs runs alone
    mgr.add_provider(bc)
    mgr.initialize_all(session_id="sl-0", platform="cli", hermes_home=home, agent_context="primary")
    tools = sorted(t["name"].rsplit("_", 1)[-1] for t in mgr.get_all_tool_schemas())
    assert tools == ["crumb", "find"], tools  # crumb and crumb_find: breadcrumbs' own two tools
    with open(os.path.join(home, "breadcrumbs", "provider.log")) as f:
        line = next(x for x in f if "slot kettle" in x).split(" ", 2)[2].strip()
    print("slot reason in provider.log:", line)
    assert line == "slot kettle: not available: KETTLE_API_KEY is not set", line
    assert bc.unavailable_reason() == "KETTLE_API_KEY is not set", bc.unavailable_reason()
    mgr.shutdown_all()
    fields = {f["key"]: f for f in _memory_provider_payload("breadcrumbs", BreadcrumbsProvider())["fields"]}
    print("slot setup fields:", sorted(fields))  # 2. the dashboard: a fresh provider for each request
    assert sorted(fields) == ["slot", "slot.api_key", "slot.lid", "slot.shelf", "turn_factor"], sorted(fields)
    assert (fields["slot.api_key"]["kind"], fields["slot.api_key"]["is_set"]) == ("secret", False)
    assert fields["slot.lid"]["when"] == {"slot.shelf": "low"}, fields["slot.lid"]
    assert not _memory_provider_is_configured("breadcrumbs", BreadcrumbsProvider())  # status: the slot lacks its key
    _write_memory_provider_config_values("breadcrumbs", BreadcrumbsProvider(), {"slot.api_key": "k-1", "slot.shelf": "low"})
    assert _memory_provider_is_configured("breadcrumbs", BreadcrumbsProvider()), "the slot's key did not count"
    assert load_env().get("KETTLE_API_KEY") == "k-1", "the key is not in the slot's own env var"
    with open(os.path.join(home, "kettle.json")) as f:
        kept = json.load(f)
    print("slot save_config got:", kept)
    assert kept == {"shelf": "low", "lid": "red"}, kept  # lid: shown by shelf=low, kept at its default
    fields = {f["key"]: f for f in _memory_provider_payload("breadcrumbs", BreadcrumbsProvider())["fields"]}
    assert (fields["slot.shelf"]["value"], fields["slot.api_key"]["is_set"]) == ("low", True), fields["slot.shelf"]
    _write_memory_provider_config_values("breadcrumbs", BreadcrumbsProvider(),  # a save that changes nothing
                                         {k: f["value"] for k, f in fields.items() if f["kind"] != "secret"})
    with open(os.path.join(home, "kettle.json")) as f:
        assert json.load(f) == kept, "a save that changed nothing changed the slot's values"
    os.environ["KETTLE_API_KEY"] = load_env()["KETTLE_API_KEY"]  # 3. as a new process: kettle gets its key from .env
    print("OK: setup fields under slot., the key in KETTLE_API_KEY, the values in kettle's save_config, "
          "the reason in provider.log")


def slot_check():
    """breadcrumbs with a slot: a second memory provider, loaded by Hermes's own loader, runs THRU it."""
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        os.makedirs(os.path.join(home, "plugins", "kettle"))
        with open(os.path.join(home, "plugins", "kettle", "__init__.py"), "w") as f:
            f.write(SLOT_PLUGIN)
        os.makedirs(os.path.join(home, "breadcrumbs"))
        with open(os.path.join(home, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"slot": "kettle"}, f)
        os.environ.pop("KETTLE_API_KEY", None)
        slot_setup_check(home)
        mgr, bc = MemoryManager(), BreadcrumbsProvider()
        mgr.add_provider(bc)
        mgr.initialize_all(session_id="sl-1", platform="cli", hermes_home=home, agent_context="primary")
        tools = sorted(t["name"] for t in mgr.get_all_tool_schemas())
        print("slot tools:", tools)
        assert len(tools) == 1 and tools[0].endswith("kettle_search"), tools  # THRU: the slot's tools only
        T = tools[0][: -len("kettle_search")]
        assert mgr.build_system_prompt() == "## Kettle memory\nThe kettle memory is on.", mgr.build_system_prompt()
        recall = mgr.prefetch_all("where is the walrus kettle?", session_id="sl-1")
        print("slot prefetch:\n" + recall)
        assert recall == "- the walrus kettle is on the top shelf (kettle memory)", recall  # byte for byte
        hit = json.loads(mgr.handle_tool_call(T + "kettle_search", {"q": "kettle"}))
        assert hit == {"result": "kettle hit for kettle"}, hit
        mgr.sync_all("the slot gets this turn too", "ok", session_id="sl-1")
        assert mgr.flush_pending(timeout=10), "sync did not drain"
        with open(os.path.join(home, "kettle-sync.txt")) as f:
            assert "the slot gets this turn too" in f.read()
        assert bc.session_crumb("sl-1"), "breadcrumbs kept no record under the slot"
        mgr.shutdown_all()
        os.environ.pop("KETTLE_API_KEY", None)
        print("OK: a provider loaded by Hermes ran THRU breadcrumbs' slot (recall, tool, prompt, sync), "
              "and breadcrumbs kept its record under it")


def ace_check():
    """ACE through Hermes's own engine loader (context.engine) and its per-request selection hook."""
    import logging
    import types
    from agent.context_engine import ContextEngine
    from agent.conversation_loop import _apply_context_engine_selection
    from plugins.context_engine import load_context_engine
    from breadcrumbs import ace, cli
    from test_ace import SYSTEM, chat
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        with open(os.path.join(home, "config.yaml"), "w") as f:
            f.write("memory:\n  provider: breadcrumbs\ncontext:\n  engine: breadcrumbs\n")
        assert cli.main(["install-hermes", home]) in (0, None)
        eng = load_context_engine("breadcrumbs")
        assert isinstance(eng, ContextEngine) and eng.name == "breadcrumbs", eng
        eng.update_model("test-model", 32000)
        eng.on_session_start("ace-1", hermes_home=home)
        eng.target_tokens = 8000
        req = [SYSTEM] + chat(40)
        agent = types.SimpleNamespace(context_compressor=eng, session_id="ace-1")
        out = _apply_context_engine_selection(agent, req, req, req[-1], logger=logging.getLogger("ace_check"))
        assert out is not req and out[1]["content"].startswith(ace.LAYER_HEAD), str(out[1]["content"])[:80]
        assert out[2:] == req[len(req) - len(out) + 2:]
        print("ACE: %d -> %d messages, %d -> %d bytes, layer %d crumbs, status %s" % (
            len(req), len(out), ace.request_bytes(req), ace.request_bytes(out), eng.layer_crumbs,
            {k: v for k, v in eng.get_status().items() if k.startswith("ace_")}))
        from agent.agent_runtime_helpers import sanitize_api_messages
        from test_ace import long_turn, pairs
        eng.on_session_start("ace-2", hermes_home=home)
        req = [SYSTEM] + chat(2) + long_turn(60, think=200)  # one turn of an agent at work passes the target
        out = _apply_context_engine_selection(agent, req, req, req[9], logger=logging.getLogger("ace_check"))
        assert out[1]["content"].startswith(ace.LAYER_HEAD) and out[2]["role"] == "assistant", out[2]["role"]
        assert pairs(out)[0] == pairs(out)[1] and out[-4:] == req[-4:], "a step lost its call or its result"
        clean = sanitize_api_messages([dict(m) for m in out])
        assert len(clean) == len(out) and pairs(clean) == pairs(out), (len(clean), len(out))
        print("ACE long turn: %d -> %d messages, %d -> %d bytes; Hermes's sanitizer dropped no message" % (
            len(req), len(out), ace.request_bytes(req), ace.request_bytes(out)))
        eng.close()
        print("OK: Hermes loaded ACE by context.engine, and its request hook sent the layer")


def handbook_check():
    """The dispatch handbook through Hermes's own memory loader, skills index and skill_view; then the bc
    command that it gives, run as a delegate_task child would run it, beside the live store."""
    import subprocess
    from agent.prompt_builder import _plugin_skill_prompt_rows
    from plugins.memory import load_memory_provider
    from tools.skills_tool import skill_view
    from breadcrumbs import cli, hermes
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        with open(os.path.join(home, "config.yaml"), "w") as f:
            f.write("memory:\n  provider: breadcrumbs\n")
        assert cli.main(["install-hermes", home]) in (0, None)
        prov = load_memory_provider("breadcrumbs")  # the active provider: Hermes registers its skills
        assert prov is not None and prov.handbook == "breadcrumbs:dispatch", getattr(prov, "handbook", None)
        rows = dict(_plugin_skill_prompt_rows(set(), None, None, None))
        assert rows.get("breadcrumbs:dispatch") == hermes.HANDBOOK_ABOUT, rows
        page = json.loads(skill_view("breadcrumbs:dispatch"))
        text = page.get("content", "")
        cmd = 'cd "%s/../../.." && python3 -m breadcrumbs bc' % os.path.dirname(hermes.HANDBOOK)
        assert "least manufactured intent" in text and cmd in text, sorted(page)
        mgr = MemoryManager()
        mgr.add_provider(prov)
        mgr.initialize_all(session_id="hb-1", platform="cli", hermes_home=home, agent_context="primary")
        assert 'skill_view("breadcrumbs:dispatch")' in mgr.build_system_prompt()
        mgr.sync_all("where is the walrus kettle", "the walrus kettle is on the top shelf", session_id="hb-1")
        assert mgr.flush_pending(timeout=10), "sync did not drain"
        out = subprocess.run("%s %s 1" % (cmd, prov.session_crumb("hb-1")), shell=True, capture_output=True,
                             text=True)
        assert out.returncode == 0 and "the walrus kettle is on the top shelf" in out.stdout, out.stderr
        mgr.shutdown_all()
        print("OK: Hermes listed breadcrumbs:dispatch, served it with its folder filled in, the prompt block "
              "named it, and its bc command showed the session beside the live store")


def island_check():
    """Islands through Hermes's own code. The memory loader runs the plugin's register(); model_tools posts
    tool calls in two codebases, and turn_finalizer's reply transform adds the red offer once (the counts
    are in <home>/breadcrumbs/islands.json). Hermes's slash-command table holds /island: `/island status`
    shows the islands and their checks; with a path it gives the codebase its own profile (a path skips the
    Yes or No question that a bare /island asks). The handoff
    runs with a recorded Hermes (FakeHermes of tests/test_island.py), not with Hermes's CLI: this check
    never runs `hermes profile create` or `hermes config set`, and subprocess.run fails while the handoff
    runs. The home folder and the Hermes root are temp folders."""
    import subprocess
    import types
    from pathlib import Path
    from unittest import mock
    from agent.turn_finalizer import apply_llm_output_transform
    from hermes_cli.plugins import get_plugin_command_handler
    from model_tools import _emit_post_tool_call_hook
    from plugins.memory import load_memory_provider
    from breadcrumbs import cli, island
    from test_island import FakeHermes  # tests/ is the folder of this script
    saved = {k: os.environ.get(k) for k in ("HOME", "HERMES_HOME")}
    with tempfile.TemporaryDirectory() as top:  # top is the home folder here
        top = os.path.realpath(top)
        home, a, b = (os.path.join(top, d) for d in (".hermes", "work/a", "work/b"))
        for d in (home, a + "/.git", b + "/.git"):
            os.makedirs(d)
        os.environ.update(HOME=top, HERMES_HOME=home)  # the Hermes CLI of the last step runs in this home too
        try:
            with open(os.path.join(home, "config.yaml"), "w") as f:
                f.write("memory:\n  provider: breadcrumbs\n")
            with mock.patch.object(island, "HOME", top):
                assert cli.main(["install-hermes", home]) in (0, None)
                assert load_memory_provider("breadcrumbs") is not None
                for i, root in enumerate([a] * 3 + [b] * 3):
                    _emit_post_tool_call_hook(function_name="read_file",
                                              function_args={"path": "%s/f%d.py" % (root, i)},
                                              result="{}", session_id="is-1")
                agent = types.SimpleNamespace(session_id="is-1", model="test-model", platform="cli")
                out, changed, before = apply_llm_output_transform(agent, "Done.", turn_id="t-1")
                again = apply_llm_output_transform(
                    types.SimpleNamespace(**dict(vars(agent), _llm_output_transform=None)), "Next.", turn_id="t-2")
                print(out)
                assert changed and before == "Done.", (changed, before)
                assert out.startswith("Done.\n\n> [!CAUTION]\n> \U0001F534 **breadcrumbs: this chat mixes two"), out
                assert "`~/work/a` (3 calls) and `~/work/b` (3 calls)" in out and "`/island ~/work/b`" in out, out
                assert "spacegraph" not in out.lower() and "plugins install" not in out, out
                assert again == ("Next.", False, None), again  # one offer per new codebase, not one per reply
                chat = island.load(home)["is-1"]
                assert chat["roots"] == {a: 3, b: 3} and chat["warned"] == [a, b], chat  # no island: both codebases
                print("OK: Hermes's tool hook found two codebases in one chat, its reply hook added the red offer "
                      "once, and islands.json holds the counts")

                handler = get_plugin_command_handler("island")
                assert handler is not None, "Hermes has no /island"
                assert (handler.__module__, handler.__name__) == (island.__name__, "island_command"), \
                    "Hermes's /island is not this plugin's command: %r" % (handler,)
                text = handler("status")
                assert "This profile (`default`)" in text and "Checks: every island is sound." in text, text
                recorded = FakeHermes(Path(home))
                with mock.patch.object(island, "run_hermes", recorded), \
                        mock.patch.object(subprocess, "run", side_effect=AssertionError("the handoff ran a command")):
                    made = handler(b)
                print(made)
                assert "`~/work/b` has its own profile now: `b`" in made and "`hermes -p b`" in made, made
                assert [(c[:6], h) for c, h in recorded.calls] == [
                    (["profile", "create", "b", "--clone-from", "default", "--no-alias"], home),
                    (["-p", "b", "config", "set", "terminal.cwd", b], os.path.join(home, "profiles", "b"))], recorded.calls
                text = handler("status")
                assert "  ~/work/b -> b" in text and "Checks: every island is sound." in text, text
                assert "breadcrumbs islands of all profiles" in handler("--all")
            shell = subprocess.run([sys.executable, "-m", "hermes_cli.main", "islands", "status"], capture_output=True,
                                   text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=top)
            print("hermes islands status: exit %d, %s" % (shell.returncode, (shell.stdout + shell.stderr).strip()[-300:]))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        print("OK: Hermes's /island showed the islands and their checks, and its handoff gave the two Hermes "
              "commands (profile create, config set) to a recorded Hermes, which gave ~/work/b its own profile")


def wire_check():
    """Each model's options on Hermes's OpenAI chat-completions wire (dials.py). First, with Hermes's own auth
    registry and provider profiles: each provider that logs in with OAuth keeps the request that Hermes builds,
    and each API-key provider takes the options. Then tests/openai_wire_e2e.py runs one AIAgent turn against a
    local server, first without the patch and then with it. It runs as a child process, because AIAgent reads
    HERMES_HOME when Hermes loads."""
    import subprocess
    from breadcrumbs import dials
    from hermes_cli.auth import PROVIDER_REGISTRY
    from providers import get_provider_profile
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        rows = PROVIDER_REGISTRY.items()
        logins = sorted(k for k, row in rows if str(getattr(row, "auth_type", "")).startswith("oauth"))
        keys = sorted(k for k, row in rows if getattr(row, "auth_type", "") == "api_key")
        assert logins and keys, (logins, keys)
        for k in logins:
            prof = get_provider_profile(k)
            assert dials.oauth({"provider_name": k}) and (prof is None or dials.oauth({"provider_profile": prof})), k
        for k in keys:
            prof = get_provider_profile(k)
            assert not dials.oauth({"provider_name": k}), k
            assert prof is None or not dials.oauth({"provider_profile": prof}), (k, prof.auth_type)
        print("OAuth logins that keep Hermes's request: %s; API-key providers that take the options: %d" % (
            ", ".join(logins), len(keys)))
    run = subprocess.run([sys.executable, os.path.join(REPO, "tests", "openai_wire_e2e.py")],
                         capture_output=True, text=True, timeout=240)
    print(run.stdout.strip())
    assert run.returncode == 0 and "OK:" in run.stdout, (run.returncode, run.stderr[-2000:])


def scan_check():
    """Hermes's install and update scanner (tools/plugin_guard.py) on a clean copy of the plugin, as
    `plugins install` and `plugins update` run it. A "caution" verdict stops an update with no terminal
    ("BLOCKED ... Use --force"), so the plugin must scan "safe"."""
    import shutil
    from pathlib import Path
    from tools.plugin_guard import scan_plugin, should_allow_plugin_install
    skip = shutil.ignore_patterns(".git", ".notes", "var", "__pycache__", "data")
    with tempfile.TemporaryDirectory() as top:
        copy = os.path.join(top, "breadcrumbs")
        shutil.copytree(REPO, copy, ignore=skip)
        result = scan_plugin(Path(copy), source="https://github.com/breadcrumbs-agency/hermes-breadcrumb-plugin.git")
    high = ["%s %s:%s" % (f.severity, f.file, f.line) for f in result.findings if f.severity in ("high", "critical")]
    assert result.verdict == "safe", (result.verdict, high)
    assert should_allow_plugin_install(result)[0] is True
    print("OK: Hermes's plugin scanner gave the plugin a safe verdict (%d notes, none high)" % len(result.findings))


if __name__ == "__main__":
    main()
    scan_check()
    slot_check()
    ace_check()
    wire_check()
    handbook_check()
    island_check()
