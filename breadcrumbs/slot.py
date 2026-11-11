"""Slot: one other memory provider on top of breadcrumbs (a pass-through: THRU, not beside).

Hermes runs one external memory provider. With a slot (config.json "slot": <provider name>, or
BREADCRUMBS_SLOT), breadcrumbs is that provider, and each Hermes call goes THRU it to the slot: Hermes gets
the slot's recall, recall line, prompt block, compress text, tools and identity as they are, and the slot
gets each hook as Hermes would give it. breadcrumbs stacks under it: it keeps each turn, each memory write
and each text that the slot recalls (as a "slot" crumb, meta src = the slot's name), with the receipts, so
the copies stay after the slot is gone. It adds nothing of its own, except its crumb tools and prompt
block when its context engine (ACE) runs. A failing slot is logged and skipped; it never fails breadcrumbs.

Setup goes THRU too. Hermes's setup (`hermes memory setup`, the dashboard) asks the active provider, and with a
slot that is breadcrumbs. breadcrumbs shows the slot's fields after its own ones, under FIELD_PREFIX, and gives
each value under that prefix back to the slot. The slot's reason (unavailable_reason) goes to provider.log.
"""
import inspect
import json
import os

FIELD_PREFIX = "slot."  # a setup field of the slot, as breadcrumbs shows it: slot.<the slot's own key>


def load(name, skills=True):
    """The Hermes memory provider called name, or None (outside Hermes, or when it does not load). Hermes
    registers the skills of memory.provider only, and with a slot that is breadcrumbs: the slot registers its
    own skills here, as it did when it was memory.provider. A setup never runs the slot, so it loads the slot
    with skills=False (Hermes's rule: a look at a provider registers nothing)."""
    try:
        from plugins.memory import load_memory_provider
    except ImportError:
        return None
    return load_memory_provider(name, register_skills=skills)


def stored_values(name, home):
    """The setup values that Hermes keeps for the provider called name, read as Hermes's dashboard reads them
    (hermes_cli/web_server_memory.py, _read_memory_provider_existing_values). Each source wins over the one
    before it: the legacy config.yaml memory.provider_config, <home>/<name>.json, <home>/<name>/config.json,
    config.yaml memory.<name>, and for holographic config.yaml plugins.hermes-memory-store."""
    values = {}
    for path in (os.path.join(home, name + ".json"), os.path.join(home, name, "config.json")):
        try:
            with open(path, encoding="utf-8-sig") as f:
                got = json.load(f)
            values.update(got if isinstance(got, dict) else {})
        except (OSError, ValueError):
            pass
    try:
        from hermes_cli import config
        cfg = (getattr(config, "load_config_readonly", None) or config.load_config)()
    except Exception:  # outside Hermes
        return values
    memory = cfg.get("memory") if isinstance(cfg, dict) else None
    memory = memory if isinstance(memory, dict) else {}
    if isinstance(memory.get(name), dict):
        values.update(memory[name])
    if isinstance(memory.get("provider_config"), dict):
        values = {**memory["provider_config"], **values}
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    if name == "holographic" and isinstance(plugins, dict) and isinstance(plugins.get("hermes-memory-store"), dict):
        values.update(plugins["hermes-memory-store"])
    return values


def _own(provider, hook):
    """True when the provider has the hook of its own (not the empty one of Hermes's MemoryProvider)."""
    fn = getattr(type(provider), hook, None)
    if fn is None:
        return False
    try:
        from agent.memory_provider import MemoryProvider
    except ImportError:
        return True
    return fn is not getattr(MemoryProvider, hook, None)


def _fit(fn, kwargs):
    """The keyword arguments that fn takes (all of them when it takes **kwargs), so a provider with an
    older, shorter signature still gets the hook."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind is p.VAR_KEYWORD for p in params):
        return kwargs
    names = {p.name for p in params}
    return {k: v for k, v in kwargs.items() if k in names}


class Slot:
    def __init__(self, name, provider, log):
        self.name, self.provider, self.log = name, provider, log
        self.tools = {}  # wire name -> the slot's own tool name

    @classmethod
    def load_named(cls, name, log, setup=False):
        """The provider called name, loaded but not started (Hermes lists the tools of a provider before it
        initializes it). None when it is missing or unavailable: the log then gets the slot's own reason. A
        setup (setup=True) loads an unavailable provider too: the setup is where the user gives what it lacks."""
        try:
            provider = load(name, skills=False) if setup else load(name)
            if provider is not None and (setup or provider.is_available()):
                return cls(name, provider, log)
            reason = cls(name, provider, log).call("unavailable_reason", default="") if provider is not None else ""
            reason = reason.strip() if isinstance(reason, str) else ""
            log("slot %s: not available%s" % (name, ": " + reason if reason else ""))
        except Exception as e:  # a slot never fails breadcrumbs
            log("slot %s: load failed: %r" % (name, e))
        return None

    def start(self, session_id, kwargs):
        """Initialize the provider with breadcrumbs' own arguments. False when it fails."""
        try:
            self.provider.initialize(session_id, **_fit(self.provider.initialize, kwargs))
            return True
        except Exception as e:  # a slot never fails breadcrumbs
            self.log("slot %s: start failed: %r" % (self.name, e))
            return False

    def call(self, hook, *args, default=None, **kwargs):
        """provider.hook(*args, **kwargs), or default when the provider lacks the hook or it fails."""
        fn = getattr(self.provider, hook, None)
        if fn is None:
            return default
        try:
            return fn(*args, **_fit(fn, kwargs))
        except Exception as e:  # a slot never fails breadcrumbs
            self.log("slot %s %s failed: %r" % (self.name, hook, e))
            return default

    def schemas(self, prefix, taken):
        """The slot's tools under wire names (prefix + name, see hermes.TOOL_PREFIX). A name that
        breadcrumbs uses stays with breadcrumbs."""
        out, self.tools = [], {}
        for t in self.call("get_tool_schemas", default=None) or []:
            name = str(t.get("name", "")) if isinstance(t, dict) else ""
            wire = name if name.startswith(prefix) else prefix + name
            if name and wire not in taken:
                self.tools[wire] = name
                out.append(dict(t, name=wire))
        return out

    def fields(self, stored):
        """The slot's setup fields under FIELD_PREFIX. The key of a field and the keys that it names (when,
        default_from) get the prefix, as Hermes's setup and dashboard look them up in the same form. Hermes
        reads the stored values of the active provider (breadcrumbs), not the slot's: so a value that the slot
        keeps (stored, see stored_values) becomes the default, and a setup that leaves the field alone keeps it."""
        out = []
        for f in self.call("get_config_schema", default=None) or []:
            key = str(f.get("key") or "").strip() if isinstance(f, dict) else ""
            if not key:
                continue
            f = dict(f)
            f["key"] = FIELD_PREFIX + key
            if isinstance(f.get("when"), dict):
                f["when"] = {FIELD_PREFIX + str(k): v for k, v in f["when"].items()}
            if isinstance(f.get("default_from"), dict) and f["default_from"].get("field"):
                f["default_from"] = dict(f["default_from"], field=FIELD_PREFIX + str(f["default_from"]["field"]))
            if not f.get("secret") and stored.get(key) not in (None, ""):
                d = f.get("default")  # Hermes infers a kind from a typed default: keep that kind
                if not (f.get("type") or f.get("kind")) and isinstance(d, (bool, int, float)):
                    f["type"] = "boolean" if isinstance(d, bool) else "integer" if isinstance(d, int) else "number"
                f["default"] = stored[key]
            out.append(f)
        return out

    def save(self, values, hermes_home):
        """Give the slot its setup values (keys without FIELD_PREFIX): to its own save_config, or, when it has
        none, to config.yaml memory.<name>, where Hermes's dashboard keeps the values of such a provider."""
        if _own(self.provider, "save_config"):
            self.call("save_config", values, hermes_home)
            return
        try:
            from hermes_cli.config import load_config, save_config
            cfg = load_config()
            memory = cfg.get("memory")
            if not isinstance(memory, dict):
                memory = cfg["memory"] = {}
            current = memory.get(self.name)
            memory[self.name] = {**(current if isinstance(current, dict) else {}), **values}
            save_config(cfg)
        except Exception as e:  # a slot never fails breadcrumbs (outside Hermes there is no config.yaml)
            self.log("slot %s save_config failed: %r" % (self.name, e))
