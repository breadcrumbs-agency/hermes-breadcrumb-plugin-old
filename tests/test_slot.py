"""A slot: another Hermes memory provider on top of breadcrumbs (a pass-through). Hermes runs one external
memory provider; with a slot, breadcrumbs is that provider, and each call goes THRU it to the other one:
Hermes sees the other provider's answers as before, and breadcrumbs keeps a copy under it."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, hermes, slot as slot_mod
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.hermes import BreadcrumbsProvider


class FakeSlot:
    """A memory provider with its own recall, tool and hooks. It records each call; with fail=True every
    hook after initialize raises."""
    name = "fake"

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def _note(self, hook, *args, **kwargs):
        self.calls.append((hook, args, kwargs))
        if self.fail and hook != "initialize":
            raise RuntimeError("slot down")

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self._note("initialize", session_id, **kwargs)

    def system_prompt_block(self):
        return "## Fake memory\nUse fake_search."

    def prefetch(self, query, *, session_id=""):
        self._note("prefetch", query, session_id=session_id)
        return "- the walrus kettle is blue (fake memory)"

    def sync_turn(self, user_content, assistant_content, *, session_id=""):  # an older signature: no messages
        self._note("sync_turn", user_content, assistant_content, session_id=session_id)

    def get_tool_schemas(self):
        return [{"name": "fake_search", "description": "search the fake memory",
                 "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self._note("handle_tool_call", tool_name, args)
        return json.dumps({"result": "fake hit for " + args.get("q", "")})

    def on_memory_write(self, action, target, content, metadata=None):
        self._note("on_memory_write", action, target, content)

    def on_pre_compress(self, messages):
        self._note("on_pre_compress", messages)
        return "fake insight"

    def on_session_end(self, messages):
        self._note("on_session_end", messages)

    def recall_status(self):
        return STATUS

    def identity_signature(self):
        return {"fake.user": "u1"}

    def shutdown(self):
        self._note("shutdown")


STATUS = ("fake", 2)  # the slot's own recall line (any object: breadcrumbs passes it as it is)
ACE_ON = "context:\n  engine: breadcrumbs\n"  # config.yaml that selects breadcrumbs' context engine
# A census of Nov 8 2026, read with ast only: the methods of Hermes's MemoryProvider base class, and the hooks
# that 70 memory providers of the Hermes catalog define.
CENSUS_ABC = ["name", "is_available", "initialize", "unavailable_reason", "system_prompt_block", "prefetch",
              "queue_prefetch", "recall_status", "sync_turn", "get_tool_schemas", "handle_tool_call",
              "shutdown", "on_turn_start", "identity_signature", "on_session_end", "on_session_switch",
              "on_pre_compress", "on_delegation", "get_config_schema", "save_config", "on_memory_write",
              "backup_paths", "on_profile_created", "on_profile_renamed", "read_config_fields",
              "write_config_fields"]
CENSUS_USED = ["backup_paths", "get_config_schema", "get_tool_schemas", "handle_tool_call",
               "identity_signature", "initialize", "is_available", "name", "on_delegation", "on_memory_write",
               "on_pre_compress", "on_session_end", "on_session_switch", "on_turn_start", "prefetch",
               "queue_prefetch", "recall_status", "save_config", "shutdown", "sync_turn", "system_prompt_block",
               "unavailable_reason"]

# A slot's setup fields: a secret in its own env var, a field with choices, a field that only cloud mode shows,
# and a field whose default comes from another field.
SETUP = [
    {"key": "api_key", "description": "Fake API key", "secret": True, "required": True, "env_var": "FAKE_API_KEY"},
    {"key": "mode", "description": "Where the fake memory runs", "default": "local", "choices": ["local", "cloud"]},
    {"key": "region", "description": "The cloud region", "default": "eu", "when": {"mode": "cloud"}},
    {"key": "shelf", "description": "The shelf", "default": "top",
     "default_from": {"field": "mode", "map": {"cloud": "high"}}},
]


class SetupSlot(FakeSlot):
    """A slot with setup fields and a reason for when it is not available. Its save_config keeps the values in
    <home>/fake.json, a file where Hermes reads the stored values of a provider called fake."""

    def __init__(self, available=True, **kwargs):
        super().__init__(**kwargs)
        self.available, self.saved = available, []

    def is_available(self):
        return self.available

    def unavailable_reason(self):
        self._note("unavailable_reason")
        return "FAKE_API_KEY is not set"

    def get_config_schema(self):
        self._note("get_config_schema")
        return [dict(f) for f in SETUP]

    def save_config(self, values, hermes_home):
        self._note("save_config", values, hermes_home)
        self.saved.append((dict(values), hermes_home))
        with open(os.path.join(hermes_home, "fake.json"), "w") as f:
            json.dump(values, f)


class PlainSetupSlot(FakeSlot):
    """A slot with setup fields and no save_config of its own (Hermes keeps its values in config.yaml)."""

    def get_config_schema(self):
        return [dict(f) for f in SETUP[1:]]


def every_hook(hooks):
    """A SetupSlot class with each hook in hooks. Each hook adds its name to reached, then answers as SetupSlot
    answers (None for a hook that SetupSlot lacks)."""
    class EveryHook(SetupSlot):
        def __init__(self):
            super().__init__()
            self.reached = set()

    def hook(name):
        inner = getattr(SetupSlot, name, None)

        def fn(self, *args, **kwargs):
            self.reached.add(name)
            return inner(self, *args, **kwargs) if callable(inner) else None
        return fn
    for h in hooks:
        if h != "name":  # breadcrumbs keeps its own name: Hermes keys the provider by it
            setattr(EveryHook, h, hook(h))
    return EveryHook


def drive(p, home):
    """Call each hook of Hermes's MemoryProvider on breadcrumbs, as Hermes calls it. The last one is shutdown."""
    p.get_tool_schemas()
    p.system_prompt_block()
    p.on_turn_start(1, "where is the kettle?")
    p.prefetch("where is the kettle?", session_id="s1")
    p.recall_status()
    p.handle_tool_call(hermes.TOOL_PREFIX + "fake_search", {"q": "kettle"})
    p.sync_turn("where is the kettle?", "on the top shelf", session_id="s1")
    p.queue_prefetch("where is the kettle?", session_id="s1")
    p.on_memory_write("add", "memory", "the kettle is on the top shelf")
    p.on_delegation("find the kettle", "found it", child_session_id="c1")
    p.on_pre_compress([])
    p.on_session_switch("s2", parent_session_id="s1", reset=False)
    p.identity_signature()
    p.backup_paths()
    p.get_config_schema()
    p.save_config({"slot.mode": "cloud"}, home)
    getattr(p, "unavailable_reason", lambda: "")()  # before this branch, breadcrumbs had no own method
    p.on_session_end([])
    p.shutdown()


class SlotTest(unittest.TestCase):
    def make(self, fake, config=None, yaml=""):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "breadcrumbs")
        os.makedirs(root)
        with open(os.path.join(root, "config.json"), "w") as f:
            json.dump({"slot": "fake"} if config is None else config, f)
        with open(os.path.join(self.tmp.name, "config.yaml"), "w") as f:
            f.write(yaml)
        p = BreadcrumbsProvider()
        with mock.patch.object(slot_mod, "load", lambda name: fake if name == "fake" else None):
            p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        return p

    def hooks(self, fake):
        return [c[0] for c in fake.calls]

    def receipt(self, p, sid):
        return get_crumb(p.store, p.refs.get("rcpt-" + sid))

    def test_the_recall_is_the_slot_recall_byte_for_byte(self):
        fake = FakeSlot()
        p = self.make(fake)
        p.sync_turn("the walrus kettle is green", "noted", session_id="s0")  # breadcrumbs' own match
        text = p.prefetch("where is the walrus kettle?", session_id="s1")
        self.assertEqual(text, "- the walrus kettle is blue (fake memory)")  # THRU: nothing beside it
        self.assertEqual(fake.calls[-1], ("prefetch", ("where is the walrus kettle?",), {"session_id": "s1"}))

    def test_breadcrumbs_keeps_the_slot_text_under_it_with_one_receipt(self):
        p = self.make(FakeSlot())
        text = p.prefetch("where is the walrus kettle?", session_id="s1")
        receipt = self.receipt(p, "s1")
        (cid,) = receipt.meta["items"]  # the slot's text only: breadcrumbs added none of its own
        self.assertEqual(receipt.meta["bytes"], len(text.encode("utf-8")))
        crumb = get_crumb(p.store, cid)
        self.assertEqual((crumb.kind, crumb.meta["src"]), ("slot", "fake"))
        shown = json.loads(p.handle_tool_call(hermes.TOOL_PREFIX + "crumb", {"id": cid, "level": 1.0}))["text"]
        self.assertIn(text, shown)  # the exact bytes, kept

    def test_the_same_slot_text_is_kept_once(self):
        p = self.make(FakeSlot())
        p.prefetch("walrus kettle?", session_id="s1")
        first = self.receipt(p, "s1").meta["items"]
        p.prefetch("the walrus kettle again?", session_id="s1")
        second = self.receipt(p, "s1").meta["items"]
        self.assertEqual(first, second)
        rows = p.index.db.execute("SELECT COUNT(*) FROM rows WHERE crumb = ?", (first[0],)).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_the_recall_line_is_the_slot_line(self):
        p = self.make(FakeSlot())
        p.prefetch("where is the walrus kettle?", session_id="s1")
        self.assertIs(p.recall_status(), STATUS)

    def test_the_prompt_block_and_the_compress_text_are_the_slot_text(self):
        p = self.make(FakeSlot())
        p.sync_turn("the walrus kettle is green", "noted", session_id="s1")
        self.assertEqual(p.system_prompt_block(), "## Fake memory\nUse fake_search.")
        self.assertEqual(p.on_pre_compress([]), "fake insight")
        self.assertIsNone(p.refs.get("rcpt-s1"))  # no receipt for a text that breadcrumbs did not give

    def test_the_identity_is_the_slot_identity_before_any_start(self):
        # The gateway keys its cached agent by the identity of an instance that it never starts.
        fake = FakeSlot()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"slot": "fake"}, f)
        with mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name}), \
                mock.patch.object(slot_mod, "load", lambda name: fake if name == "fake" else None):
            self.assertEqual(BreadcrumbsProvider().identity_signature(), {"fake.user": "u1"})
        self.assertEqual(self.hooks(fake).count("initialize"), 0)

    def test_with_ace_on_the_crumb_tools_and_block_come_too(self):
        # ACE's layer shows crumb handles, and the model opens them with crumb: they are part of the
        # steady state, so breadcrumbs shows its own tools and block next to the slot's.
        p = self.make(FakeSlot(), yaml=ACE_ON)
        names = sorted(t["name"] for t in p.get_tool_schemas())
        self.assertEqual(names, sorted(hermes.TOOL_PREFIX + n for n in ("crumb", "crumb_find", "fake_search")))
        block = p.system_prompt_block()
        self.assertIn("Breadcrumbs memory", block)
        self.assertTrue(block.endswith("\n\n## Fake memory\nUse fake_search."))
        self.assertEqual(p.prefetch("where is the walrus kettle?", session_id="s1"),
                         "- the walrus kettle is blue (fake memory)")  # the recall stays the slot's

    def test_the_slot_registers_its_own_skills(self):
        # Hermes registers skills only for memory.provider, and with a slot that is breadcrumbs.
        seen = []
        memory = mock.Mock(load_memory_provider=lambda name, register_skills=None: seen.append(register_skills))
        with mock.patch.dict(sys.modules, {"plugins": mock.Mock(memory=memory), "plugins.memory": memory}):
            slot_mod.load("fake")
        self.assertEqual(seen, [True])

    def test_every_hook_reaches_the_slot(self):
        fake = FakeSlot()
        p = self.make(fake)
        p.sync_turn("hello slot", "hi there", session_id="s1", messages=[])
        p.on_memory_write("add", "memory", "a fact for both")
        p.on_pre_compress([])
        p.on_session_end([])
        p.shutdown()
        for hook in ("initialize", "sync_turn", "on_memory_write", "on_pre_compress", "on_session_end", "shutdown"):
            self.assertIn(hook, self.hooks(fake))
        sync = fake.calls[self.hooks(fake).index("sync_turn")]
        self.assertEqual(sync[1:], (("hello slot", "hi there"), {"session_id": "s1"}))  # no messages: older signature

    def test_breadcrumbs_still_stores_the_turn_with_a_slot(self):
        p = self.make(FakeSlot())
        p.sync_turn("the kettle question", "the kettle answer", session_id="s1")
        self.assertIsNotNone(p.session_crumb("s1"))

    def test_the_slot_tools_are_offered_and_answered(self):
        fake = FakeSlot()
        p = self.make(fake)
        wire = hermes.TOOL_PREFIX + "fake_search"
        self.assertEqual([t["name"] for t in p.get_tool_schemas()], [wire])  # THRU: the slot's tools only
        self.assertEqual(json.loads(p.handle_tool_call(wire, {"q": "kettle"})), {"result": "fake hit for kettle"})
        self.assertEqual(fake.calls[-1][1][0], "fake_search")  # the slot gets its own tool name

    def test_the_slot_tools_are_listed_before_initialize_as_hermes_asks_for_them(self):
        # MemoryManager.add_provider reads get_tool_schemas() before initialize_all() and routes each tool
        # call by that list, so the slot must be known then (it starts later, in initialize).
        fake = FakeSlot()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"slot": "fake"}, f)
        p = BreadcrumbsProvider()
        with mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name}), \
                mock.patch.object(slot_mod, "load", lambda name: fake if name == "fake" else None):
            names = [t["name"] for t in p.get_tool_schemas()]
            p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        self.assertIn(hermes.TOOL_PREFIX + "fake_search", names)
        self.assertEqual(self.hooks(fake).count("initialize"), 1)
        out = json.loads(p.handle_tool_call(hermes.TOOL_PREFIX + "fake_search", {"q": "k"}))
        self.assertEqual(out, {"result": "fake hit for k"})

    def test_a_failing_slot_never_fails_breadcrumbs(self):
        fake = FakeSlot(fail=True)
        p = self.make(fake)
        p.sync_turn("the walrus kettle is green", "noted", session_id="s0")
        self.assertIsNotNone(p.session_crumb("s0"))  # breadcrumbs still keeps the turn
        self.assertEqual(p.prefetch("walrus kettle", session_id="s1"), "")  # THRU: what the slot gave
        self.assertIn("error", json.loads(p.handle_tool_call(hermes.TOOL_PREFIX + "fake_search", {"q": "x"})))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "provider.log")) as f:
            log = f.read()
        self.assertIn("slot fake prefetch failed", log)
        self.assertIn("slot fake sync_turn failed", log)

    def test_a_missing_slot_is_logged_and_breadcrumbs_runs_alone(self):
        p = self.make(None, config={"slot": "nowhere"})
        self.assertIsNone(p.slot)
        self.assertEqual(sorted(t["name"] for t in p.get_tool_schemas()),
                         sorted(hermes.TOOL_PREFIX + n for n in ("crumb", "crumb_find")))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "provider.log")) as f:
            self.assertIn("slot nowhere: not available", f.read())

    def test_setup_offers_and_saves_the_slot(self):
        p = self.make(FakeSlot(), config={})
        self.assertIn("slot", [f["key"] for f in p.get_config_schema()])
        p.save_config({"slot": " fake "}, self.tmp.name)
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json")) as f:
            self.assertEqual(json.load(f)["slot"], "fake")

    def read(self, *path):
        with open(os.path.join(self.tmp.name, *path)) as f:
            return f.read()

    def loads(self, fake):
        """Hermes's loader, faked: it gives fake for the name fake (a setup load also names the skills flag)."""
        return mock.patch.object(slot_mod, "load", lambda name, skills=True: fake if name == "fake" else None)

    def test_setup_shows_the_fields_of_a_slot_that_is_not_available(self):
        # The usual case: the slot lacks its key, and the setup is where the user gives it.
        fake = SetupSlot(available=False)
        p = self.make(fake)
        with self.loads(fake):
            self.assertIn("slot.api_key", [f["key"] for f in p.get_config_schema()])
            p.save_config({"slot.mode": "cloud"}, self.tmp.name)
        self.assertEqual(fake.saved, [({"mode": "cloud"}, self.tmp.name)])
        self.assertIsNone(p.slot)  # the setup does not start it: it is still not available

    def test_a_setup_load_registers_no_skills(self):
        # A setup reads fields and writes values, and never runs the slot. Hermes's loader registers the skills
        # of a provider only when it runs (a look at a provider leaves no side effects).
        seen = []
        memory = mock.Mock(load_memory_provider=lambda name, register_skills=None: seen.append(register_skills))
        with mock.patch.dict(sys.modules, {"plugins": mock.Mock(memory=memory), "plugins.memory": memory}):
            slot_mod.Slot.load_named("fake", lambda msg: None, setup=True)
        self.assertEqual(seen, [False])

    def test_setup_shows_the_slot_fields_under_the_slot_prefix(self):
        schema = self.make(SetupSlot()).get_config_schema()
        self.assertEqual([f["key"] for f in schema],
                         ["turn_factor", "slot", "slot.api_key", "slot.mode", "slot.region", "slot.shelf"])
        fields = {f["key"]: f for f in schema}
        self.assertEqual(fields["slot.api_key"], dict(SETUP[0], key="slot.api_key"))  # the slot's own env var
        self.assertEqual(fields["slot.region"]["when"], {"slot.mode": "cloud"})  # a named field gets the prefix too
        self.assertEqual(fields["slot.shelf"]["default_from"], {"field": "slot.mode", "map": {"cloud": "high"}})

    def test_setup_values_under_the_prefix_go_thru_to_the_slot(self):
        fake = SetupSlot()
        p = self.make(fake)
        p.save_config({"turn_factor": 2, "slot": "fake", "slot.mode": "cloud", "slot.region": "us"}, self.tmp.name)
        self.assertEqual(fake.saved, [({"mode": "cloud", "region": "us"}, self.tmp.name)])
        self.assertEqual(json.loads(self.read("breadcrumbs", "config.json")), {"slot": "fake", "turn_factor": 2.0})

    def test_a_stored_slot_value_is_the_default_of_its_field(self):
        # Hermes reads stored setup values by the name of the active provider (breadcrumbs). A save fills each
        # field that the user leaves alone with that value or with the default. So the slot's stored value
        # must be the default, or the save writes the slot's defaults over its settings.
        fake = SetupSlot()
        p = self.make(fake)
        p.save_config({"slot.mode": "cloud", "slot.region": "us"}, self.tmp.name)
        fields = {f["key"]: f for f in p.get_config_schema()}
        self.assertEqual([fields[k]["default"] for k in ("slot.mode", "slot.region", "slot.shelf")], ["cloud", "us", "top"])
        p.save_config({k: f["default"] for k, f in fields.items() if k.startswith("slot.") and not f.get("secret")},
                      self.tmp.name)
        self.assertEqual(fake.saved[-1][0], {"mode": "cloud", "region": "us", "shelf": "top"})

    def test_stored_values_are_read_in_the_order_of_hermes(self):
        # hermes_cli/web_server_memory.py _read_memory_provider_existing_values: the legacy memory.provider_config
        # first, then <name>.json, <name>/config.json and memory.<name>; the holographic provider last.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = tmp.name
        os.makedirs(os.path.join(home, "fake"))
        for path, values in ((("fake.json",), {"a": "json", "b": "json", "c": "json"}),
                             (("fake", "config.json"), {"b": "dir", "c": "dir"})):
            with open(os.path.join(home, *path), "w") as f:
                json.dump(values, f)
        conf = {"memory": {"fake": {"c": "yaml"}, "provider_config": {"a": "legacy", "d": "legacy"}},
                "plugins": {"hermes-memory-store": {"e": "plugins"}}}
        config = mock.Mock(load_config=lambda: json.loads(json.dumps(conf)), spec=["load_config"])
        with mock.patch.dict(sys.modules, {"hermes_cli": mock.Mock(config=config), "hermes_cli.config": config}):
            self.assertEqual(slot_mod.stored_values("fake", home), {"a": "json", "b": "dir", "c": "yaml", "d": "legacy"})
            self.assertEqual(slot_mod.stored_values("holographic", home), {"a": "legacy", "d": "legacy", "e": "plugins"})

    def test_a_slot_without_its_own_save_config_keeps_its_values_where_hermes_keeps_them(self):
        # Hermes's fallback for such a provider: config.yaml memory.<name> (_save_memory_provider_native_config)
        p = self.make(PlainSetupSlot())
        conf, saved = {"memory": {"provider": "breadcrumbs", "fake": {"shelf": "low"}}}, []
        config = mock.Mock(load_config=lambda: json.loads(json.dumps(conf)), save_config=saved.append)
        with mock.patch.dict(sys.modules, {"hermes_cli": mock.Mock(config=config), "hermes_cli.config": config}):
            p.save_config({"slot.mode": "cloud", "turn_factor": 1}, self.tmp.name)
        self.assertEqual(saved, [{"memory": {"provider": "breadcrumbs", "fake": {"shelf": "low", "mode": "cloud"}}}])
        self.assertEqual(json.loads(self.read("breadcrumbs", "config.json"))["turn_factor"], 1.0)

    def test_the_reason_of_an_unavailable_slot_goes_to_the_log_and_thru(self):
        fake = SetupSlot(available=False)
        p = self.make(fake)
        self.assertIsNone(p.slot)
        self.assertTrue(p.is_available())  # breadcrumbs runs alone, and keeps its record
        with self.loads(fake):
            self.assertEqual(p.unavailable_reason(), "FAKE_API_KEY is not set")
        self.assertIn("slot fake: not available: FAKE_API_KEY is not set", self.read("breadcrumbs", "provider.log"))

    def test_without_a_slot_setup_and_reason_stay_breadcrumbs_own(self):
        p = self.make(None, config={})
        self.assertEqual([f["key"] for f in p.get_config_schema()], ["turn_factor", "slot"])
        p.save_config({"turn_factor": 3, "slot.mode": "cloud"}, self.tmp.name)
        self.assertEqual(json.loads(self.read("breadcrumbs", "config.json")), {"turn_factor": 3.0})
        self.assertEqual(p.unavailable_reason(), "")

    def test_setup_reaches_the_slot_before_any_start(self):
        # hermes memory setup and the dashboard load the provider, and they never start it
        fake = SetupSlot()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"slot": "fake"}, f)
        with mock.patch.dict(os.environ, {"HERMES_HOME": self.tmp.name}), self.loads(fake):
            p = BreadcrumbsProvider()
            self.assertIn("slot.mode", [f["key"] for f in p.get_config_schema()])
            p.save_config({"slot.mode": "cloud"}, self.tmp.name)
        self.assertEqual(fake.saved, [({"mode": "cloud"}, self.tmp.name)])
        self.assertEqual(self.hooks(fake).count("initialize"), 0)

    def test_a_failing_slot_setup_never_fails_breadcrumbs(self):
        p = self.make(SetupSlot(fail=True))
        self.assertEqual([f["key"] for f in p.get_config_schema()], ["turn_factor", "slot"])
        p.save_config({"turn_factor": 2, "slot.mode": "cloud"}, self.tmp.name)
        self.assertEqual(json.loads(self.read("breadcrumbs", "config.json"))["turn_factor"], 2.0)
        log = self.read("breadcrumbs", "provider.log")
        self.assertIn("slot fake get_config_schema failed", log)
        self.assertIn("slot fake save_config failed", log)

    def test_every_hook_that_the_census_providers_use_reaches_the_slot(self):
        # CENSUS_USED: the hooks that 70 memory providers of the Hermes catalog use. Each one
        # reaches the slot when Hermes calls it on breadcrumbs. `name` stays breadcrumbs' own: Hermes keys the
        # provider by it.
        used = set(CENSUS_USED)
        fake = every_hook(CENSUS_ABC)()
        drive(self.make(fake), self.tmp.name)
        self.assertEqual(sorted(used - fake.reached), ["name"])


class SlotCommandTest(unittest.TestCase):
    """python -m breadcrumbs slot PROFILE [PROVIDER] [--off]: the pass-through installer. Hermes's config is a
    fake here (cli.hermes_config); the profile home is a temp folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf, self.sets = {"memory.provider": "mem0"}, []

    def fake_hermes(self, profile, *args):
        if args[0] == "get":
            return self.conf.get(args[1], "")
        self.conf[args[1]] = args[2]
        self.sets.append(args[1:])
        return ""

    def run_cli(self, *argv):
        out = io.StringIO()
        with mock.patch.object(cli, "hermes_config", self.fake_hermes), \
                mock.patch.object(cli, "profile_home", lambda profile: self.tmp.name), \
                contextlib.redirect_stdout(out):
            code = cli.main(["slot", *argv])
        return code, out.getvalue()

    def config(self):
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json")) as f:
            return json.load(f)

    def test_the_current_provider_moves_into_the_slot_and_breadcrumbs_takes_its_place(self):
        code, out = self.run_cli("work")
        self.assertEqual(code, 0)
        self.assertEqual(self.config()["slot"], "mem0")
        self.assertEqual(self.conf["memory.provider"], "breadcrumbs")
        self.assertIn("mem0", out)

    def test_off_gives_the_slot_its_place_back(self):
        self.run_cli("work")
        code, out = self.run_cli("work", "--off")
        self.assertEqual(code, 0)
        self.assertEqual(self.conf["memory.provider"], "mem0")
        self.assertNotIn("slot", self.config())

    def test_a_named_slot_is_used_as_given(self):
        self.conf["memory.provider"] = ""
        self.run_cli("work", "honcho")
        self.assertEqual(self.config()["slot"], "honcho")
        self.assertEqual(self.conf["memory.provider"], "breadcrumbs")

    def test_without_another_provider_nothing_changes(self):
        self.conf["memory.provider"] = "breadcrumbs"
        code, out = self.run_cli("work")
        self.assertEqual((code, self.sets), (0, []))
        self.assertIn("no slot", out)

    def test_the_other_settings_stay(self):
        os.makedirs(os.path.join(self.tmp.name, "breadcrumbs"))
        with open(os.path.join(self.tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"turn_factor": 8}, f)
        self.run_cli("work")
        self.assertEqual(self.config(), {"turn_factor": 8, "slot": "mem0"})

    def test_a_dev_provider_name_is_kept(self):
        self.run_cli("work", "--name", "bcdev")
        self.assertEqual(self.conf["memory.provider"], "bcdev")

    def test_a_profile_without_a_provider_reads_as_none(self):
        # `hermes config get` exits 1 with "Config key not set: KEY" when the key is not set
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="Config key not set: memory.provider\n")
        with mock.patch.object(cli.subprocess, "run", lambda *a, **k: done):
            self.assertEqual(cli.hermes_config("work", "get", "memory.provider"), "")
            with self.assertRaises(SystemExit):
                cli.hermes_config("work", "set", "memory.provider", "x")  # a failed set still stops


if __name__ == "__main__":
    unittest.main()
