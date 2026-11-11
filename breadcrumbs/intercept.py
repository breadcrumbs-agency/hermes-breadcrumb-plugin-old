"""The context intercept: ACE takes Hermes's built-in compressor, Hermes-wide.

Hermes runs its built-in ContextCompressor wherever an agent has no plugin engine: a profile with no
context.engine, safe-worker mode, a clone_for_agent() that fails, an engine that does not load, the gateway's
/compress with no plugin engine, and a later Hermes that reads context.engine from a new place. ACE counts
each one as its own error. install() runs at plugin load and wraps the class once for the whole process; one
gateway serves every profile, so it holds them all. A compressor takes an Ace in its own profile's home at its
first hook call: select_context (each request), compress (a compaction) and update_from_response (the model's
counts) go to that Ace, and _generate_summary (the prepared-compaction pass calls it too) gives None, so no
model writes a summary. When ACE cannot fold, the built-in compacts with its own fallback text, also with no
model call. Each capture writes one Hermes warning and one ace.log line (act=intercept).

A profile is held only when its memory provider is a breadcrumbs plugin: the layer needs the crumb tool to
open a crumb, and a fold there would hide turns that the model cannot open. Other profiles keep the built-in
(one warning each). The intercept is opt-in: it changes Hermes's compressor for every profile of the gateway,
through private core imports, so a profile gets it only with "intercept": true in <home>/breadcrumbs/config.json.
A profile that selects ACE (context.engine: breadcrumbs) does not need it.
"""
import functools
import json
import logging
import os

from . import ace

HOOKS = ("select_context", "compress", "update_from_response", "_generate_summary")
SAVED = "_breadcrumbs_intercept"  # on the class: each hook before the intercept, and whether the class had its own
KEY = "_breadcrumbs_ace"  # on a compressor: its Ace (False: its profile opted out)
HOST_ENV = "HERMES_PLUGIN_HOST_PROCESS"  # "1" in Hermes's plugin host (hermes_cli/plugin_isolation.py)
log = logging.getLogger(__name__)


def _compressor_class():
    try:
        import agent  # noqa: F401  (Hermes's package: absent in tests and in the CLI)
    except ImportError:
        return None
    try:
        from agent.context_compressor import ContextCompressor
        return ContextCompressor
    except Exception as e:  # an update moved it: say so, the routes are then not held
        log.warning("breadcrumbs intercept: no Hermes compressor (%s): routes that skip ACE are not held", e)
        return None


def opted_in(home):
    """True when <home>/breadcrumbs/config.json says "intercept": true. Absent, false or bad: the profile keeps
    Hermes's built-in compressor."""
    try:
        with open(os.path.join(home, "breadcrumbs", "config.json"), encoding="utf-8") as f:
            return json.load(f).get("intercept") is True
    except (OSError, ValueError, AttributeError):
        return False


def serves_crumbs(home):
    """True when the profile's memory provider is a breadcrumbs plugin (a shim or a copy of this repo)."""
    name = ace.setting(home, "memory", "provider")
    try:
        with open(os.path.join(home, "plugins", name or "-", "__init__.py"), encoding="utf-8") as f:
            return "breadcrumbs" in f.read(2048)
    except OSError:
        return False


def held(home):
    return serves_crumbs(home) and opted_in(home)


def ace_of(cc):
    """The Ace of a built-in compressor (made at its first hook call, in the profile home of that call)."""
    a = cc.__dict__.get(KEY)
    if a is None:
        home = ace._home()
        a = cc.__dict__[KEY] = ace.Ace() if held(home) else False
        if a:
            a.home = home
            log.warning("breadcrumbs intercept: Hermes ran its built-in compressor (home %s, session %s): ACE takes it",
                        home, getattr(cc, "_session_id", "") or "-")
        else:
            log.warning("breadcrumbs intercept: %s has no breadcrumbs memory (or no intercept opt-in): its built-in compacts",
                        home)
    if a:
        sid = str(getattr(cc, "_session_id", "") or "")
        if a.session != sid:
            a.on_session_start(sid)
        a.context_length, a.threshold_tokens = cc.context_length, cc.threshold_tokens
        if not getattr(a, "_captured", False) and a._open():
            a._captured = True
            a._log("intercept", 0)
    return a


def uninstall(cls=None):
    """Put the built-in hooks back."""
    cls = cls or _compressor_class()
    saved = cls and cls.__dict__.get(SAVED)
    if not saved:
        return
    for hook, (fn, own) in saved.items():
        if own:
            setattr(cls, hook, fn)
        elif hook in cls.__dict__:
            delattr(cls, hook)
    delattr(cls, SAVED)


def install(cls=None):
    """Wrap the hooks of cls (default: Hermes's built-in compressor), again after a reload: the class keeps
    the first originals. Returns the hooks that the class does not have (empty: all held), or None outside
    Hermes."""
    if cls is None and os.environ.get(HOST_ENV) == "1":  # plugins.isolation: host puts this code in another process
        log.warning("breadcrumbs intercept: plugin host process: the agents' compressor is not here, so routes "
                    "that skip ACE are not held")
        return None
    cls = cls or _compressor_class()
    if cls is None:
        return None
    saved = cls.__dict__.get(SAVED) or {h: (getattr(cls, h), h in cls.__dict__) for h in HOOKS if hasattr(cls, h)}
    setattr(cls, SAVED, saved)
    base = {h: fn for h, (fn, _) in saved.items()}

    def select_context(self, request_messages, **kw):
        a = ace_of(self)
        return a.select_context(request_messages, **kw) if a else base["select_context"](self, request_messages, **kw)

    def compress(self, messages, *args, **kw):
        a, out = None, messages
        try:
            a = ace_of(self)
            out = a.compress(messages) if a else messages
        except Exception:
            log.exception("breadcrumbs intercept: ACE did not fold; the built-in compacts")
        if a and out is not messages:
            a._sent = ace.request_bytes(out)
            self.compression_count = getattr(self, "compression_count", 0) + 1  # Hermes's status reads it
            return out
        return base["compress"](self, messages, *args, **kw)

    def update_from_response(self, usage, *args, **kw):
        out = base["update_from_response"](self, usage, *args, **kw)
        a = self.__dict__.get(KEY)
        if a:  # the model's own count calibrates the Ace
            try:
                a.update_from_response(usage)
            except Exception:
                log.exception("breadcrumbs intercept: ACE did not take the model's counts")
        return out

    def _generate_summary(self, *args, **kw):
        a = self.__dict__.get(KEY)  # a prepared-pass worker may have none: its profile decides
        out = not held(ace._home()) if a is None else a is False
        return base["_generate_summary"](self, *args, **kw) if out else None

    wrappers: dict = {f.__name__: f for f in (select_context, compress, update_from_response, _generate_summary)}
    for h, fn in base.items():
        setattr(cls, h, functools.update_wrapper(wrappers[h], fn))
    missing = [h for h in HOOKS if h not in base]
    if missing:
        log.warning("breadcrumbs intercept: this Hermes compressor has no %s: those routes are not held",
                    ", ".join(missing))
    return missing
