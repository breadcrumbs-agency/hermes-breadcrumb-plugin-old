"""Hermes memory provider: breadcrumbs as the external `memory.provider`.

Install into a profile:  python -m breadcrumbs install-hermes --profile NAME
That writes a small plugin shim to $HERMES_HOME/plugins/breadcrumbs/ which
imports this module. All state lives in $HERMES_HOME/breadcrumbs/ (store,
index.db, refs). Nothing leaves the machine.

Model tools (barebones parameters):
  crumb(id, level)   show a breadcrumb at a level from 0 (handle) to 1 (all)
  crumb_find(q, k)   search every breadcrumb; returns handles to expand
"""

import json
import os
import queue
import re
import sqlite3
import threading
import time

from . import dials, handles, tags
from .crc32c import BACKEND
from .crumbs import Crumb, get_crumb, put_crumb
from .history import Refs, commit
from .horizon import Controller, decay, split
from .index import Index
from .ingest import leaves, put_rows, put_tree, put_turn
from .lines import put_text, split_lines
from .slot import FIELD_PREFIX, Slot, stored_values
from .store import Store
from .summarize import MAX_INPUT, TRIES, PromptTooLong, closed_spans, default_runner, plan, shrink, store_summary
from .view import Trail, label as trail_label
from .zoom import Zoom

try:  # inside Hermes
    from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
except Exception:  # outside Hermes (tests, CLI): same methods, no base class
    MemoryProvider, RecallStatus = object, None
    _TRIVIAL = re.compile(r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|hi|hey|hello|k|done|next|continue)\W*$", re.I)

    def is_trivial_prompt(text):
        s = (text or "").strip()
        return not s or s.startswith("/") or bool(_TRIVIAL.match(s))

SYSTEM_BLOCK = """## Breadcrumbs memory
Past turns and memory entries are kept exactly, under content ids. A line "\u25b8 <id> ..." (handle) or "\u22ef <id>:a-b" (omitted lines) is a breadcrumb.
- crumb(id, level): show a breadcrumb at a level from 0 (handle only) to 1 (all of it, verbatim). A handle shows the full size as "\u00b7Nt" (N tokens). Pick a level that shows a few hundred tokens, about 400 / N, then expand only the parts you need. Below level 1 a text shows its key words in text order (the most distinct words first, then the meaningful words); "\u2026" marks dropped words. Grammar words and exact wording come back only at level 1. A "sum" crumb is a summary that a small model wrote; its sources are exact. A session or span handle ends with cue words after "|": the words it uses more than the rest of the memory, in order of first use.
- crumb_find(q): search all breadcrumbs by words. It returns each hit under the handle of its session.
The ids are the provenance. Cite them when you use a recalled fact."""

TOOLS = [
    {"name": "crumb",
     "description": "Show a breadcrumb at a level from 0 (handle only) to 1 (all of it, verbatim). "
                    "id is a crumb id from a \u25b8 handle, or a range id:a-b from a \u22ef marker. "
                    "The handle shows the full size as \u00b7Nt: a level of about 400 / N shows a few hundred tokens. "
                    "Below level 1 a text shows its key words in order (\u2026 marks dropped words); level 1 gives the exact text. "
                    "The result holds more handles and markers: expand them with more small calls.",
     "parameters": {"type": "object",
                    "properties": {"id": {"type": "string", "description": "crumb id, text id, or id:a-b"},
                                   "level": {"type": "number", "minimum": 0, "maximum": 1}},
                    "required": ["id", "level"]}},
    {"name": "crumb_find",
     "description": "Search all breadcrumbs (past turns, memory entries) by words. Returns handles, each hit "
                    "under its session handle; expand one with crumb(id, level).",
     "parameters": {"type": "object",
                    "properties": {"q": {"type": "string"},
                                   "k": {"type": "integer", "minimum": 1, "maximum": 20}},
                    "required": ["q"]}},
]

# With hive peers (config.json "hive", see hive.py) crumb_find also asks other hosts, each within a budget.
HIVE_TOOLS = [dict(TOOLS[0], description=TOOLS[0]["description"] + " A hive hit id@peer/store opens from that "
                   "peer; each part is checked against its CRC32C."),
              dict(TOOLS[1], description=TOOLS[1]["description"] + " It also asks the hive peers (other hosts); "
                   "a peer that cannot answer in its time budget passes.")]
HIVE_BLOCK = ("\n- With hive peers, crumb_find also shows a \"\u25c7 hive\" line and hits id@peer/store from other "
              "hosts. Each peer answers within a time budget or passes. Open a hit with crumb(id@peer/store, level).")

# The delegation and dispatch handbook goes with the tool descriptions: a plugin skill that Hermes lists as
# <plugin>:dispatch (register_handbook), named again in the prompt block under the tools.
HANDBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills", "dispatch", "SKILL.md")
HANDBOOK_ABOUT = "Use when you delegate or dispatch work. Send exact crumbs."
HANDBOOK_LINE = ("\n- Before you give work to a helper (delegate_task, message_agent, a cron job), read "
                 "skill_view(\"%s\"). It tells how to send exact crumbs, not your own words for them.")


def register_handbook(ctx, provider):
    """Register the handbook with the tools. A Hermes without plugin skills gets no skill and no prompt line."""
    if not hasattr(ctx, "register_skill"):
        return
    try:
        ctx.register_skill("dispatch", HANDBOOK, HANDBOOK_ABOUT)
    except Exception:  # a name clash, a missing file or another Hermes's signature: the memory works without it
        return
    plugin = getattr(ctx, "name", None) or getattr(getattr(ctx, "manifest", None), "name", None)
    provider.handbook = "%s:dispatch" % (plugin or "breadcrumbs")


def register_handles(ctx):
    """Register the hook of the dial family: the handle line of each child in the result of delegate_task
    (handles.py). The hook returns at once for every other tool, and for delegate_task while the dial is off. A
    Hermes without plugin hooks gets none."""
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("transform_tool_result", handles.transform_tool_result)


def register_plugin(ctx, provider):
    """What both entry points (the repository root and the shim) add to Hermes after the provider: its
    handbook, the intercept (ACE in the built-in compressor), the islands (the offer when a chat mixes two
    codebases, /island and its checks), the usage log (one line of numbers for each model request) and the hook
    of the family. Each entry point registers the provider and ACE itself: Hermes finds a user plugin's memory
    provider and context engine by the texts "register_memory_provider" and "register_context_engine" in its
    __init__.py (plugins/memory and plugins/context_engine in Hermes). Two calls wrap code of Hermes, and each
    acts only where a profile opts in: intercept.install() puts ACE in the built-in compressor ("intercept": true),
    and dials.install() wraps the OpenAI chat-completions build, so the options of a model in "models" reach the
    wire (dials.py). Nothing else here replaces a class or a function of Hermes."""
    from . import dials, intercept, island, usage_log
    from .ace import _home
    register_handbook(ctx, provider)
    intercept.install()
    dials.install(_home)
    island.register(ctx)
    usage_log.register(ctx)
    register_handles(ctx)


PREFETCH_HEAD = "Breadcrumbs from earlier sessions (expand with crumb(id, level)):\n"
COMPRESS_HEAD = "Breadcrumbs of this session (exact sources; expand with crumb(id, level)):\n"

# Hermes on the Anthropic OAuth wire sends every tool as "mcp__<name>" and maps a reply back
# only through the global tool registry, which never holds memory-provider tools
# (agent/transports/anthropic.py:12-23). A provider tool named "crumb" then fails as
# "mcp__crumb does not exist". A name that already starts with "mcp__" survives the round
# trip unchanged, so that is the default. handle_tool_call accepts both forms.
TOOL_PREFIX = os.environ.get("BREADCRUMBS_TOOL_PREFIX", "mcp__")

# Prefetch priority = BM25 score x max(PRIOR_FLOOR, decay by age and idle time). An old crumb
# keeps PRIOR_FLOOR of its weight, so the permanent record stays reachable; a crumb the model
# expanded (crumb tool) counts as accessed at that time.
AGE_HALF = dials.env_value("age_half_days") * 86400  # the defaults; a profile's config.json may set its own (dials.py)
IDLE_HALF = dials.env_value("idle_half_days") * 86400
PRIOR_FLOOR = 0.05
# A turn "overflows" when Hermes compacts the context, or when the recall text injected in this
# session is more than RECALL_SHARE of the context. The Controller holds overflows at 2%..5%.
RECALL_SHARE = dials.env_value("recall_share")
# Dialogue-neighbor smoothing for search: a turn next to a strong hit gets NEIGHBOR x that score.
NEIGHBOR = dials.env_value("neighbor")
# A full turn cuts find and open; the note says so (NOTE: the bytes kept free for it).
CUT, NOTE = "[cut: %s; this turn's memory budget of %d B has %d B left. The next user turn gets a new budget.]\n", 160


def _spawn(target, name):
    """An unstarted daemon thread. Inside Hermes it comes from spawn_context_thread, so it keeps
    the profile's contextvars (a bare thread starts with an empty context)."""
    try:
        from agent.memory_provider import spawn_context_thread
    except ImportError:
        return threading.Thread(target=target, name=name, daemon=True)
    return spawn_context_thread(target, name=name)


def _context_bytes(messages):
    n = 0
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            n += len(c.encode("utf-8", "replace"))
        elif isinstance(c, list):
            n += sum(len(p["text"].encode("utf-8", "replace")) for p in c
                     if isinstance(p, dict) and isinstance(p.get("text"), str))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                n += len(str((tc.get("function") or {}).get("arguments", "")))
    return n


def _config(root):
    """The provider's own config (<root>/config.json, written by hermes memory setup); {} when absent."""
    try:
        with open(os.path.join(root, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _ref_name(prefix, session_id):
    return prefix + re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "none")[:100]


# A ref as a handle or the dispatch handbook writes it: an id (eight hex digits, then .N after a CRC clash) or
# id:a-b (lines of a document). A longer run of letters or digits is no ref.
REF = re.compile(r"(?<![0-9A-Za-z_.])([0-9a-f]{8}(?:\.\d+)?)(?::\d{1,9}-\d{1,9})?(?![0-9A-Za-z_])")


def _worker_text(brief, report, parent, child="", car="", root=""):
    """The text of a worker receipt: its session ids (and the CAR and the root id, when known), then the brief and
    the report word for word. Each part starts on a line of its own."""
    def lines(text):
        return text if not text or text.endswith("\n") else text + "\n"
    head = "".join("%s: %s\n" % (k, v) for k, v in (("parent", parent), ("child", child), ("car", car), ("root", root)) if v)
    return "worker\n" + head + "brief:\n" + lines(brief) + "report:\n" + lines(report)


def _turn_rows(messages):
    """The tool steps of this turn: the rows after the last user row that call tools or carry a tool
    result. The user text and the final answer come from sync_turn's own arguments."""
    rows = list(messages or ())
    last = max((i for i, m in enumerate(rows) if isinstance(m, dict) and m.get("role") == "user"), default=-1)
    return [m for m in rows[last + 1:] if isinstance(m, dict)
            and (m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls")))]


class BreadcrumbsProvider(MemoryProvider):
    def __init__(self, name="breadcrumbs"):
        self._name = name
        self._lock = threading.RLock()
        self.ctl = Controller(budget=dials.env_value("prefetch_tokens"))
        self.neighbor, self.recall_share, self.age_half, self.idle_half = NEIGHBOR, RECALL_SHARE, AGE_HALF, IDLE_HALF
        self.clock = time.time
        self.store = None
        self.session_id = ""
        self.writable = False
        self._recalled = 0
        self._turns = {}  # session id -> user turns stored (the turn index of a context event)
        # One memory budget for each user turn: recall, find and open share turn_factor x the recall
        # budget. 0 = no turn budget. _spent = (session, turn, bytes used in that turn).
        self.turn_factor = dials.env_value("turn_factor")
        self._spent = ("", -1, 0)
        self._recall_bytes = 0  # recall text injected into this session's context so far
        self._compacted = False
        self.summarizer = None  # a small local model (see summarize.py); None = no summaries
        self.trail = Trail()  # the crumbs the model opened in this session (a zoom stack, see view.py)
        self._jobs, self._worker, self._queued, self._stop = None, None, set(), False
        self.slot = None  # the memory provider on top of this one: each call goes THRU (see slot.py)
        self._slot_tried = False  # the slot was looked up (before initialize when Hermes lists the tools first)
        self._setup = None  # the slot for setup and for its reason, loaded (not started) when it does not run
        self.hive = None  # remote stores (hive.py), from <root>/config.json "hive"; None = local only
        self.handbook = None  # the dispatch skill's name when Hermes registered it (register_handbook)
        self.family = False  # the dial family: at on_delegation, export the child's session to a CAR (family.py)

    @property
    def budget(self):
        """The recall budget in tokens, set by the steady-state controller."""
        return self.ctl.budget

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def unavailable_reason(self):
        """The slot's reason, THRU ("" without a slot: breadcrumbs needs nothing). Hermes asks for it only when
        is_available() is False, and breadcrumbs is always available (it keeps its record when its slot is
        down). So the slot's load also writes the reason to provider.log when the slot is not available."""
        slot = self._setup_slot()
        got = slot.call("unavailable_reason", default="") if slot is not None else ""
        return got if isinstance(got, str) else ""

    def initialize(self, session_id, **kwargs):
        home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        self.root = os.path.join(str(home), "breadcrumbs")
        self.store = Store(os.path.join(self.root, "store"))
        self.index = Index(os.path.join(self.root, "index.db"))
        self.refs = Refs(os.path.join(self.root, "refs"))
        self.zoom = Zoom(self.store, summaries=self.index.summary_of, cues=self.index.signature,
                         words=self.index.tiers)
        self.session_id = session_id or ""
        self.trail = Trail(session=self.session_id)
        self.writable = kwargs.get("agent_context") in (None, "", "primary")
        self.summarizer = default_runner() if self.writable else None
        self._horizon_path = os.path.join(self.root, "horizon.json")
        saved = False
        try:
            with open(self._horizon_path, encoding="utf-8") as f:
                self.ctl = Controller.from_dict(json.load(f))
            saved = True
        except (OSError, ValueError, KeyError):
            pass
        cfg = _config(self.root)
        self._dial(cfg, saved)
        name = self._slot_name(cfg)
        if self.slot is not None and self.slot.name != name:
            self.slot.call("shutdown")
            self.slot = None
        if name and self.slot is None:
            self.slot = Slot.load_named(name, self._log)
        if self.slot is not None and not self.slot.start(session_id, kwargs):
            self.slot = None
        self._slot_tried = True
        if self.hive is not None:
            self.hive.close()
            self.hive = None
        if isinstance(cfg.get("hive"), dict):  # the hive peers (see hive.py)
            from .hive import Hive
            self.hive = Hive.from_config(cfg["hive"])
        try:  # one line per agent build: which code, which tool names and which CRC32C this process uses
            with open(os.path.join(self.root, "provider.log"), "a", encoding="utf-8") as f:
                f.write("%s pid=%d file=%s tools=%s ctx=%s crc32c=%s\n" % (
                    time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), __file__,
                    ",".join(t["name"] for t in self.get_tool_schemas()), kwargs.get("agent_context"), BACKEND))
        except OSError:
            pass

    # -- prompt and tools ---------------------------------------------------
    def system_prompt_block(self):
        own = ""
        if self._shows_own():
            own = SYSTEM_BLOCK + (HIVE_BLOCK if self.hive else "") + (HANDBOOK_LINE % self.handbook if self.handbook else "")
        theirs = self.slot.call("system_prompt_block", default="") if self.slot is not None else ""
        return "\n\n".join(t for t in (own, theirs) if isinstance(t, str) and t.strip())

    def get_config_schema(self):
        own = [{"key": "turn_factor", "type": "number", "default": 0, "minimum": 0, "maximum": 64, "step": 0.5,
                "description": "One memory budget for each user turn: recall, find and open share this many "
                               "times the recall budget. 0 turns it off."},
               {"key": "slot", "type": "string", "default": "",
                "description": "Another memory provider on top of breadcrumbs (mem0, honcho, ...): each call goes "
                               "THRU breadcrumbs to it, Hermes sees its answers as before, and breadcrumbs keeps "
                               "a copy of each text under it. Empty = none."}]
        slot = self._setup_slot()  # with a slot, its own fields come next, under slot. (see Slot.fields)
        return own + (slot.fields(stored_values(slot.name, self._hermes_home())) if slot is not None else [])

    def save_config(self, values, hermes_home):
        """Write the setup values to <hermes_home>/breadcrumbs/config.json (no secrets). The values under the
        slot. prefix go THRU to the slot (see Slot.save): breadcrumbs keeps no copy of them."""
        slot = self._setup_slot(hermes_home)  # the slot whose fields the setup showed, before "slot" changes
        root = os.path.join(str(hermes_home), "breadcrumbs")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "config.json")
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        if "turn_factor" in values:
            cfg["turn_factor"] = max(0.0, float(values["turn_factor"]))
        if "slot" in values:
            cfg["slot"] = str(values["slot"] or "").strip()
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        os.replace(path + ".tmp", path)
        theirs = {str(k)[len(FIELD_PREFIX):]: v for k, v in values.items() if str(k).startswith(FIELD_PREFIX)}
        if slot is not None and theirs:
            slot.save(theirs, str(hermes_home))

    def _dial(self, cfg, saved):
        """The recall, controller and family dials of this profile (dials.py: the defaults, config.json, the
        environment). The controller keeps the budget it saved in horizon.json; prefetch_tokens is the first budget."""
        d = {r.dial.key: r.value for r in dials.resolve(cfg)}
        self.turn_factor, self.neighbor, self.recall_share = d["turn_factor"], d["neighbor"], d["recall_share"]
        self.age_half, self.idle_half = d["age_half_days"] * 86400, d["idle_half_days"] * 86400
        self.family = d["family"]
        c = self.ctl
        if d["budget_min"] < d["budget_max"]:
            c.lo, c.hi = d["budget_min"], d["budget_max"]
        if d["overflow_low"] < d["overflow_high"]:
            c.band = (d["overflow_low"], d["overflow_high"])
        c.budget = max(c.lo, min(c.hi, c.budget if saved else d["prefetch_tokens"]))

    def _slot_name(self, cfg):
        name = str(os.environ.get("BREADCRUMBS_SLOT") or cfg.get("slot") or "").strip()
        return "" if name == self._name else name

    def _hermes_home(self):
        """The profile's home: the one that initialize got, else Hermes's own (profile-scoped), else HERMES_HOME."""
        root = getattr(self, "root", None)  # set by initialize
        if root:
            return os.path.dirname(root)
        try:
            from hermes_constants import get_hermes_home
            return str(get_hermes_home())
        except Exception:
            return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")

    def _early_slot(self):
        """The slot, loaded but not started, before initialize: MemoryManager.add_provider routes tool calls by
        the tool list it reads then, before initialize_all."""
        root = os.path.join(self._hermes_home(), "breadcrumbs")
        name = self._slot_name(_config(root))
        return Slot.load_named(name, lambda msg: self._log(msg, root)) if name else None

    def _setup_slot(self, home=None):
        """The slot for its setup and its reason: the slot that runs, else the one in config.json, loaded (not
        started) also when it is not available, because the setup is where the user gives what it lacks."""
        if self.slot is not None:
            return self.slot
        root = os.path.join(str(home or self._hermes_home()), "breadcrumbs")
        name = self._slot_name(_config(root))
        if name and (self._setup is None or self._setup.name != name):
            self._setup = Slot.load_named(name, lambda msg: self._log(msg, root), setup=True)
        return self._setup if name else None

    def _the_slot(self):
        """The slot that initialize started, or before initialize the one loaded at first use: Hermes lists
        the tools and asks for backup paths, and the gateway asks for the identity, of an unstarted provider."""
        if self.slot is None and not self._slot_tried:
            self._slot_tried = True
            self.slot = self._early_slot()
        return self.slot

    def _shows_own(self):
        """breadcrumbs shows its own tools and prompt block when it runs alone. With a slot, it shows them
        only when its context engine (ACE) runs: the layer's crumb handles open with crumb."""
        if self._the_slot() is None:
            return True
        from .ace import wanted
        root = getattr(self, "root", None)  # set by initialize
        return wanted(os.path.dirname(root) if root else None)

    def get_tool_schemas(self):
        slot = self._the_slot()
        own = [dict(t, name=TOOL_PREFIX + t["name"]) for t in (HIVE_TOOLS if self.hive else TOOLS)
               ] if self._shows_own() else []
        return own + (slot.schemas(TOOL_PREFIX, {t["name"] for t in own}) if slot is not None else [])

    def backup_paths(self):
        """The slot's state outside HERMES_HOME (as ~/.honcho). Hermes's backup asks the active provider
        only, and with a slot that is breadcrumbs; breadcrumbs' own store is in HERMES_HOME already."""
        slot = self._the_slot()
        paths = slot.call("backup_paths", default=[]) if slot is not None else []
        return [str(p) for p in paths or []]

    def identity_signature(self):
        """The slot's identity, passed THRU: the gateway rebuilds its cached agent when it changes."""
        slot = self._the_slot()
        got = slot.call("identity_signature", default={}) if slot is not None else {}
        return got if isinstance(got, dict) else {}

    def handle_tool_call(self, tool_name, args, **kwargs):
        name = tool_name[5:] if tool_name.startswith("mcp__") else tool_name
        if self.slot is not None and name not in ("crumb", "crumb_find"):
            if not self.slot.tools:
                self.get_tool_schemas()
            own = self.slot.tools.get(tool_name) or self.slot.tools.get(TOOL_PREFIX + name)
            if own:
                return self.slot.call("handle_tool_call", own, args, default=json.dumps(
                    {"error": "the %s memory failed on %s" % (self.slot.name, own)}), **kwargs)
        try:
            with self._lock:
                if name == "crumb":
                    ref = str(args.get("id", ""))
                    level = float(args.get("level", 0.25))
                    zoom, head, where = self.zoom, "", ref.partition("@")[2]
                    if where:  # a hive hit: id@peer/store, rendered here from the blobs that peer sent
                        from .hive import split
                        ref, peer, sname = split(ref)
                        if self.hive is None:
                            raise KeyError("%s@%s: no hive peers" % (ref, where))
                        zoom, r = self.hive.fetch(peer, sname, ref)
                        head = "\u25c7 %s/%s: %d parts, each checked (CRC32C)%s\n" % (
                            peer, sname, len(r["blobs"]), ", %d withheld" % r["withheld"] if r["withheld"] else "")
                    text = head + zoom.render(ref, level)
                    cap, room, want = False, self._room(), len(text.encode("utf-8"))
                    if room is not None and len(text.encode("utf-8")) > room:  # the turn is full: a lower level
                        asked = level
                        level = max(0.0, min(level, (room - NOTE) / max(1, zoom.size(ref.strip()))))
                        text = head + zoom.render(ref, level)
                        if level > 0.0 and len(text.encode("utf-8")) > room - NOTE:
                            level, text = 0.0, head + zoom.handle(ref)
                        text = CUT % ("level %g asked, %.2f shown" % (asked, level), self._turn_budget(), max(0, room)) + text
                        cap = True
                    self._spend(len(text.encode("utf-8")))
                    if self.writable and where:  # the receipt names the peer; the local index has no row
                        n = len(text.encode("utf-8"))
                        self._receipt("open", [("%s@%s" % (ref.strip(), where), level, n)], n, cap=cap, want=want, text=text)
                    elif self.writable:  # the model chose to open it: that is a use
                        try:
                            self.index.touch(ref.partition(":")[0].strip(), int(self.clock()))
                            self.index.commit()
                        except sqlite3.OperationalError:  # another writer holds the index: skip the mark
                            self.index.db.rollback()
                        self._visit(ref.strip(), level)
                        n = len(text.encode("utf-8"))
                        self._receipt("open", [(ref.strip(), level, n)], n, cap=cap, want=want, text=text)
                elif name == "crumb_find":
                    q, k = str(args.get("q", "")), int(args.get("k", 8))
                    pend = self.hive.send({"op": "find", "q": q, "k": k}) if self.hive else None  # in flight
                    items, room = [], self._room()
                    text, want = self._find(q, k, items=items, room=room)
                    if pend is not None:  # the local search ran while the peers searched
                        from .hive import render
                        more, hits = render(self.hive.collect(pend), k, self.hive.ms,
                                            None if room is None else room - len(text.encode("utf-8")))
                        text, want = text + more, want + len(more.encode("utf-8"))
                        items.extend(hits)
                    self._spend(len(text.encode("utf-8")))
                    self._receipt("find", items, len(text.encode("utf-8")), q=str(args.get("q", "")),
                                  cap=text.startswith("[cut:"), want=want, text=text)
                else:
                    return json.dumps({"error": "unknown tool: %s" % tool_name})
            return json.dumps({"text": text}, ensure_ascii=False)
        except KeyError as e:
            return json.dumps({"error": "unknown id: %s" % (e.args[0] if e.args else "")})
        except (ValueError, TypeError) as e:
            return json.dumps({"error": str(e)})

    def find(self, query, k=8, exclude_session=None, items=None, room=None):
        return self._find(query, k, exclude_session, items, room)[0]

    def _find(self, query, k=8, exclude_session=None, items=None, room=None):
        """(text, want): want is the bytes of the text before a cut.
        The best hits under their session handles (with cue words), each with its best matching line.
        items: a list that gets (ref, level, bytes) for each output line, in output order (for receipts).
        room: the bytes left in this turn (None: no limit). When the hits do not fit, best lines go first
        and handles stay while they fit, so each hit shown keeps its source; a note says what was cut."""
        words = set(re.findall(r"\w+", query.lower()))
        groups, hits, parts = {}, 0, {}
        for crumb_id, ref, _score in self.index.search(query, 3 * k, nb=self.neighbor):
            sid = get_crumb(self.store, crumb_id).meta.get("sess")
            if exclude_session and sid == exclude_session:
                continue
            sess = self.refs.get(_ref_name("sess-", sid)) if sid else None
            if sess not in groups:
                groups[sess] = [self.zoom.handle(sess)] if sess else []
                parts[sess] = [(sess, 0.0)] if sess else []
            out = groups[sess]
            out.append(self.zoom.handle(crumb_id, depth=1 if sess else 0))
            parts[sess].append((crumb_id, 0.0))
            doc, _, span = ref.partition(":")
            a, _, b = span.partition("-")
            lines = self.zoom._doc_lines(doc)[int(a or 0):int(b) if b else None]
            j = max(range(len(lines)), default=None,
                    key=lambda i: len(words & set(re.findall(r"\w+", lines[i].decode("utf-8", "replace").lower()))))
            if j is not None and lines[j].strip():
                out.append(lines[j].decode("utf-8", "replace").rstrip("\n") + "\n")
                parts[sess].append(("%s:%d-%d" % (doc, int(a or 0) + j, int(a or 0) + j + 1), 1.0))
            hits += 1
            if hits >= k:
                break
        lines = [(r, lv, line) for sess, out in groups.items() for (r, lv), line in zip(parts[sess], out)]
        head, want = "", sum(len(x[2].encode("utf-8")) for x in lines)
        if room is not None and want > room:
            kept, used = [], 0
            for r, lv, line in lines:
                n = len(line.encode("utf-8"))
                if used + n <= room - NOTE:
                    kept.append((r, lv, line))
                    used += n
                elif lv == 0.0:  # a handle that does not fit: stop (a best line that does not fit is skipped)
                    break
            head = CUT % ("%d of %d lines shown" % (len(kept), len(lines)), self._turn_budget(), max(0, room))
            lines = kept
        if items is not None:
            items.extend((r, lv, len(line.encode("utf-8"))) for r, lv, line in lines)
        if not head and not lines:
            return "(no match)\n", len(b"(no match)\n")
        return head + "".join(line for _, _, line in lines), want

    # -- recall -------------------------------------------------------------
    def prefetch(self, query, *, session_id=""):
        return self._slot_recall(query, session_id) if self.slot is not None else self._recall(query, session_id)

    def _recall(self, query, session_id=""):
        self._recalled = 0
        if self.store is None or is_trivial_prompt(query):
            return ""
        with self._lock:
            current = session_id or self.session_id
            now = self.clock()
            cands, hit_refs = [], {}
            for crumb_id, ref, score in self.index.search(query, 24, nb=self.neighbor):
                meta = get_crumb(self.store, crumb_id).meta
                if meta.get("sess") != current:
                    cands.append((crumb_id, score, meta.get("ts", now)))
                    hit_refs.setdefault(crumb_id, ref)
                if len(cands) >= 6:
                    break
            if not cands:
                return ""
            cands, after = self._superseded(cands, hit_refs, current, now)
            heat = self.index.heat([c for c, _, _ in cands])
            weights = [score * max(PRIOR_FLOOR, decay(now, ts, heat.get(c, (None,))[0], self.age_half, self.idle_half))
                       for c, score, ts in cands]
            order = sorted(range(len(cands)), key=lambda i: -weights[i])
            full = 4 * self.ctl.budget  # bytes
            room = self._room(current)
            limit = full if room is None else max(0, min(full, room))
            pieces, items = self._recall_pieces(cands, order, weights, limit, after)
            if not pieces:
                return ""
            self._recalled = len(pieces)
            text = PREFETCH_HEAD + "".join(pieces)
            n = want = len(text.encode("utf-8"))
            if limit < full:  # the turn budget cut it: what it asked for
                more = self._recall_pieces(cands, order, weights, full, after)[0]
                want = len((PREFETCH_HEAD + "".join(more)).encode("utf-8"))
            self._recall_bytes += n
            self._spend(n, current)
            self._receipt("recall", items, n, current, q=query, cap=want > n, want=want, text=text)
            return text

    def _superseded(self, cands, hit_refs, current, now):
        """Each superseded candidate gives its place to its newest successor (tags.py). A successor of the
        current session stays out, as the session's own crumbs do. Returns (cands, {successor: [old, ...]})."""
        sup = tags.successors(self.index, [c for c, _, _ in cands] + list(hit_refs.values()))
        if not sup:
            return cands, {}
        have, out, after = {c for c, _, _ in cands}, [], {}
        for c, score, ts in cands:
            new = sup.get(c) or sup.get(hit_refs.get(c))
            if not new or new == c:
                out.append((c, score, ts))
                continue
            if c not in after.get(new, ()):
                after.setdefault(new, []).append(c)
            if new not in have:
                have.add(new)
                meta = get_crumb(self.store, new).meta
                if meta.get("sess") != current:
                    out.append((new, score, meta.get("ts", now)))
        return out, after

    def _recall_pieces(self, cands, order, weights, limit, after=None):
        """The recall pieces and their receipt items: each crumb zoomed into its share of limit (bytes).
        after: {successor: [old, ...]}. Each old crumb is a handle with no text, right after its successor
        (at the end when its successor is not shown). Its receipt item is the exact line, kept as a text."""
        olds = {new: [(o, tags.handle(self.zoom, o, new)) for o in old] for new, old in (after or {}).items()}
        cap = limit - sum(len(h.encode("utf-8")) for hs in olds.values() for _, h in hs)  # == limit with no tags
        pieces, items, used = [], [], 0

        def put_handle(old, h):
            data = h.encode("utf-8")
            pieces.append(h)
            ref = old  # only the primary agent writes (the receipt holds the exact line as a text)
            if self.writable:
                ref = "%s:0-%d" % (put_text(self.store, data), len(split_lines(data)))
            items.append((ref, 1.0 if self.writable else 0.0, len(data)))
            return len(data)

        for i, share in zip(order, split(max(0, cap), [weights[i] for i in order])):
            cid = cands[i][0]
            level = min(1.0, share / max(1, self.zoom.size(cid)))
            piece = self.zoom.render(cid, level)
            if used + len(piece.encode("utf-8")) > cap:
                piece, level = self.zoom.handle(cid), 0.0  # no room for content: keep the provenance
                if used + len(piece.encode("utf-8")) > cap:
                    break
            pieces.append(piece)
            items.append((cid, level, len(piece.encode("utf-8"))))
            used += len(piece.encode("utf-8"))
            for o, h in olds.pop(cid, ()):  # the crumbs that it supersedes, next to it
                n = put_handle(o, h)
                used, cap = used + n, cap + n
        for o, h in (x for hs in olds.values() for x in hs):
            if used + len(h.encode("utf-8")) > limit:
                break
            used += put_handle(o, h)
        return pieces, items

    def _slot_recall(self, query, session_id=""):
        """The slot's recall, THRU byte for byte. The primary agent keeps each text under it as a "slot" crumb
        (meta src, no time: the same text is the same crumb, indexed once) with a receipt; it stays reachable
        after the slot is gone."""
        got = self.slot.call("prefetch", query, session_id=session_id, default="")
        if not isinstance(got, str):
            return ""
        if not got.strip() or not self.writable or self.store is None:
            return got
        sid = session_id or self.session_id
        with self._lock:
            data = got.encode("utf-8")
            try:
                doc = put_text(self.store, data)
                n = len(split_lines(data))
                cid = put_crumb(self.store, Crumb("slot", self.slot.name[:60], ranges=[[doc, 0, n]],
                                                  meta={"src": self.slot.name}))  # title = source: no text twice
                self.index.add(cid, "%s:0-%d" % (doc, n), got, kind="slot")
                self.store.flush()
                self.index.commit()
            except (OSError, sqlite3.OperationalError) as e:
                self._log("slot %s recall not kept: %r" % (self.slot.name, e))
                return got
            self._recall_bytes += len(data)
            self._spend(len(data), sid)
            self._receipt("recall", [(cid, 1.0, len(data))], len(data), sid, q=query, text=got)
            return got

    def recall_status(self):
        if self.slot is not None:
            return self.slot.call("recall_status", default=None)
        if RecallStatus is None or not self._recalled:
            return None
        return RecallStatus("breadcrumbs", self._recalled)

    # -- writes -------------------------------------------------------------
    def session_crumb(self, session_id):
        return self.refs.get(_ref_name("sess-", session_id))

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        self._sync_turn(user_content, assistant_content, session_id, messages)
        if self.slot is not None:
            self.slot.call("sync_turn", user_content, assistant_content, session_id=session_id,
                           messages=messages, turn_author=turn_author)

    def _sync_turn(self, user_content, assistant_content, session_id, messages):
        if not self.writable or self.store is None:
            return
        sid = session_id or self.session_id
        with self._lock:
            ts = int(self.clock())
            ref = _ref_name("sess-", sid)
            cur = self.refs.get(ref)
            old = leaves(self.store, cur) if cur else []
            new, prev = [], old[-1] if old else None
            if user_content:
                prev = put_turn(self.store, self.index, "user", user_content, sid, ts, prev=prev)
                new.append(prev)
                self._turns.pop(sid, None)
            steps, last = put_rows(self.store, self.index, _turn_rows(messages), sid, prev=prev, ts=ts)
            new += steps
            if assistant_content:
                prev = put_turn(self.store, self.index, "assistant", assistant_content, sid, ts,
                                prev=last)  # the last indexed row: memory calls are not in the chain
                new.append(prev)
            if new:
                turns = old + new
                title = get_crumb(self.store, turns[0]).title
                sess = put_tree(self.store, "sess", title, turns, meta={"sess": sid})
                self.refs.set(ref, sess)
                commit(self.store, self.refs, "main", [sess], "turn " + sid)
            self.store.flush()
            self.index.commit()
            if new:
                self._queue_summaries(closed_spans(self.store, sess))
            ctx = _context_bytes(messages)
            crowded = ctx > 0 and self._recall_bytes > self.recall_share * ctx
            self.ctl.observe(self._compacted or crowded)
            self._compacted = False
            self._save_horizon()

    def _save_horizon(self):
        tmp = self._horizon_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.ctl.to_dict(), f)
        os.replace(tmp, self._horizon_path)

    def on_memory_write(self, action, target, content, metadata=None):
        if self.slot is not None:
            self.slot.call("on_memory_write", action, target, content, metadata=metadata)
        if not self.writable or self.store is None:
            return
        text = content or (metadata or {}).get("previous_content", "")
        if not text:
            return
        with self._lock:
            cid = put_turn(self.store, self.index, target, text, self.session_id,
                           extra={"action": action}, kind="mem")
            commit(self.store, self.refs, "memory", [cid], "%s %s" % (action, target))
            self.store.flush()
            self.index.commit()
            old = (metadata or {}).get("previous_content", "")
            if action == "replace" and content and old and old != content:
                self._supersede(old, cid)

    def _supersede(self, old_text, new):
        """Tag each mem crumb of the replaced entry as superseded by the crumb of the new entry (a real
        label: Hermes says which entry the new one replaced). The old text is never stored here."""
        try:
            for old in tags.holders(self.store, self.index, old_text, kind="mem"):
                if old != new and new not in tags.successors(self.index, [old]).values():
                    tags.supersede(self.store, self.index, self.refs, old, new)
        except (KeyError, ValueError, OSError, sqlite3.Error) as e:
            self._log("memory tag not written: %r" % (e,))

    def on_pre_compress(self, messages):
        text = self._pre_compress(messages)  # breadcrumbs' own work runs with a slot too
        return self.slot.call("on_pre_compress", messages, default="") if self.slot is not None else text

    def _pre_compress(self, messages):
        self._compacted = True  # the context reached Hermes's limit: an overflow
        self._recall_bytes = 0  # old recall text is folded into the summary
        if self.store is None:
            return ""
        sess = self.session_crumb(self.session_id)
        if not sess:
            return ""
        self._queue_summaries(closed_spans(self.store, sess) + [sess])  # for later sessions
        if self.slot is not None:
            return ""  # THRU: the slot's text goes in, so breadcrumbs gives none and keeps no receipt
        with self._lock:
            level = min(1.0, 4800 / max(1, self.zoom.size(sess)))
            text = (COMPRESS_HEAD + self.zoom.render(sess, level))[:6000]
            n = len(text.encode("utf-8"))
            self._receipt("compress", [(sess, level, n - len(COMPRESS_HEAD.encode("utf-8")))], n, ovf=True, text=text)
            return text

    # -- lifecycle ----------------------------------------------------------
    def _user_turns(self, sid):
        """The user turns stored in a session: the 0-based index of the turn that is now running."""
        if sid not in self._turns:
            sess = self.session_crumb(sid)
            self._turns[sid] = sum(1 for c in (leaves(self.store, sess) if sess else [])
                                   if get_crumb(self.store, c).meta.get("role") == "user")
        return self._turns[sid]

    def _turn_budget(self):
        """The bytes of memory for one user turn (recall, find and open together); 0 = no turn budget."""
        return int(self.turn_factor * 4 * self.ctl.budget) if self.turn_factor > 0 else 0

    def _room(self, sid=None):
        """The bytes left in the running turn, or None when there is no turn budget."""
        tb = self._turn_budget()
        if not tb:
            return None
        sid = sid or self.session_id
        spent = self._spent[2] if self._spent[:2] == (sid, self._user_turns(sid)) else 0
        return tb - spent

    def _spend(self, nbytes, sid=None):
        sid = sid or self.session_id
        turn = self._user_turns(sid)
        before = self._spent[2] if self._spent[:2] == (sid, turn) else 0
        self._spent = (sid, turn, before + int(nbytes))

    def _receipt(self, ev, items, nbytes, sid=None, ovf=False, q="", cap=False, want=None, text=None, extra=None):
        """Record one context event as an "rcpt" crumb: items = [(ref, level, bytes)] in the order the
        model saw them. kids and ranges link the crumbs and texts; meta.items, lv and b line up; meta.prev
        is the receipt before it (a hash chain under the ref rcpt-<session>); meta.q is the query of a
        recall or a find (first 120 characters). text is what the model saw, word for word: it is kept as a
        document, the last range of the receipt (meta.ctx names it). Below level 1 the words of a text come
        from the index's counts, so rendering the items again later would show another text. Receipts have no
        text in the index, so search never finds them. Only the primary agent writes.
        The event "worker" is a delegation (see _worker_receipt): it may have no items, and extra adds its
        keys to meta (a key that the receipt sets itself is kept)."""
        if not self.writable or self.store is None or not (items or ev == "worker"):
            return None
        sid = sid or self.session_id
        name = _ref_name("rcpt-", sid)
        refs = [r for r, _, _ in items]
        ranges, here = [], [x for x in refs if "@" not in x]  # a hive ref (id@peer/store) stays in meta.items
        for r in dict.fromkeys(x for x in here if ":" in x):
            doc, _, span = r.partition(":")
            a, _, b = span.partition("-")
            ranges.append([doc, int(a or 0), int(b or 0)])
        meta = {"sess": sid, "ev": ev, "ts": int(self.clock()), "turn": self._user_turns(sid), "items": refs,
                "lv": [round(float(lv), 6) for _, lv, _ in items], "b": [int(n) for _, _, n in items],
                "bytes": int(nbytes), "budget": 4 * self.ctl.budget, "ovf": bool(ovf),
                "rate": round(self.ctl.rate, 4), "band": [float(x) for x in self.ctl.band],
                "prev": self.refs.get(name) or ""}
        if q.strip():
            meta["q"] = " ".join(q.split())[:120]
        if self._turn_budget():  # only with a turn budget, so receipts without one do not change
            meta.update(cap=bool(cap), used=self._spent[2] if self._spent[:2] == (sid, meta["turn"]) else 0,
                        tb=self._turn_budget(), want=int(nbytes if want is None else want))
        for key, value in (extra or {}).items():
            meta.setdefault(key, value)
        try:  # a receipt records the context; it must never fail the recall or the tool that made it
            if text:
                data = text.encode("utf-8")
                meta["ctx"] = put_text(self.store, data)
                ranges.append([meta["ctx"], 0, len(split_lines(data))])
            rid = put_crumb(self.store, Crumb("rcpt", ev, kids=list(dict.fromkeys(x for x in here if ":" not in x)),
                                              ranges=ranges, meta=meta))
            self.refs.set(name, rid)
            self.store.flush()
        except OSError as e:
            self._log("receipt %s failed: %r" % (ev, e))
            return None
        return rid

    def _refs_in(self, text):
        """The refs that a text names and that open in this store, in the order of first mention. A ref is an id
        or id:a-b (REF); id:a-b names lines of a text, so its id must be a text. A word that only looks like an id
        (an eight-digit number, a hash that this store lacks) is no ref. Store.get also reads what another writer
        added, which `in` does not. The text keeps every id as it was written."""
        out, store = {}, self.store
        for m in REF.finditer(text):
            ref = m.group(0)
            if store is None or ref in out:
                continue
            try:
                blob = store.get(m.group(1))
            except (KeyError, ValueError):  # not in this store, or a blob that fails its CRC32C check
                continue
            if ref != m.group(1) and blob[:4] != b"SP01":  # lines of a text only: a crumb has no lines
                continue
            out[ref] = None
        return list(out)

    def _worker_receipt(self, brief, report, child="", car="", root=""):
        """Record a delegation as one "worker" receipt, on the parent's side: the brief that the lead gave the
        child, the report that came back, and both session ids (meta.sess is the parent, meta.child the child).
        car is the CID of the child's CAR and root the id of its root crumb, once the lead holds them. meta.named
        and meta.cited are the refs of the brief and of the report that open in this store; meta.items holds both
        lists. The text keeps every ref as written. A ref is a name here (level 0, the bytes of the name): the
        pieces went to the child, not into this context, and the receipt spends none of the turn budget.
        Returns the receipt id, or None when nothing was written (a read-only agent, or nothing to record)."""
        if not self.writable or self.store is None:
            return None
        brief, report = str(brief or ""), str(report or "")
        if not (brief or report or child):
            return None
        with self._lock:
            sid = self.session_id
            named, cited = self._refs_in(brief), self._refs_in(report)
            items = [(r, 0.0, len(r)) for r in dict.fromkeys(named + cited)]
            text = _worker_text(brief, report, sid, child, car, root)
            ids = {k: v for k, v in (("child", child), ("car", car), ("root", root)) if v}
            extra = {**ids, "named": named, "cited": cited}
            return self._receipt("worker", items, len(text.encode("utf-8")), sid, q=brief, text=text, extra=extra)

    def _visit(self, ref, level):
        """Put an opened crumb on the trail and save it for the views (trail.json, replaced whole)."""
        self.trail.visit(ref, level, trail_label(self.zoom, ref))
        path = os.path.join(self.root, "trail.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(self.trail.to_dict(), f, ensure_ascii=False)
        os.replace(path + ".tmp", path)

    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False, rewound=False, **kwargs):
        if self.slot is not None:
            self.slot.call("on_session_switch", new_session_id, parent_session_id=parent_session_id, reset=reset,
                           rewound=rewound, **kwargs)
        if new_session_id and new_session_id != self.session_id:
            self.trail = Trail(session=new_session_id)
        self.session_id = new_session_id or self.session_id
        if reset or kwargs.get("reason") == "compression":
            self._recall_bytes = 0

    def on_session_end(self, messages):
        if self.slot is not None:
            self.slot.call("on_session_end", messages)
        if self.store is not None:
            with self._lock:
                self.store.flush()
                self.index.commit()
                sess = self.session_crumb(self.session_id) if self.writable else None
            if sess:
                self._queue_summaries(closed_spans(self.store, sess) + [sess])

    def queue_prefetch(self, query, *, session_id=""):
        if self.slot is not None:
            self.slot.call("queue_prefetch", query, session_id=session_id)

    def on_turn_start(self, turn_number, message, **kwargs):
        if self.slot is not None:
            self.slot.call("on_turn_start", turn_number, message, **kwargs)

    def on_delegation(self, task, result, *, child_session_id="", **kwargs):
        if self.slot is not None:
            self.slot.call("on_delegation", task, result, child_session_id=child_session_id, **kwargs)
        got, root = None, ""
        if self.family and self.writable and child_session_id:
            got = self._child_car(child_session_id)
            root = self._adopt_child(got) if got else ""
        try:  # the record of a delegation must never fail it
            self._worker_receipt(task, result, child_session_id, car=got["cid"] if got else "", root=root)
        except Exception as e:
            self._log("worker receipt failed: %r" % (e,))
        if self.family and self.writable:  # one offer for each child that ran, in task order (handles.py)
            handles.offer(self.session_id, task, self._handle_line(root))

    def _adopt_child(self, got):
        """The dial family: the child's CAR comes into this store as private crumbs (share.import_car, public=False).
        Then crumb(root, level) opens the work of the child word for word, and crumb_find finds it. No export names a
        private crumb and no hive peer sees it. Returns the id of the child's session crumb here, or "" when the import
        failed: the CAR stays, the failure goes to provider.log, and a delegation never fails because of it."""
        try:
            from .share import import_car
            with self._lock:
                roots = import_car(self.store, self.index, got["car"], public=False)
        except Exception as e:
            self._log("family: child %s: import failed: %s: %s" % (got["sid"], type(e).__name__, e))
            return ""
        return roots[0] if roots else ""

    def _handle_line(self, root):
        """The handle of the child's work here, as one line, for the result of delegate_task (handles.py). "" when
        the work did not come in, and when the model has no crumb tool to open it (a slot without ACE)."""
        if not root or not self._shows_own():
            return ""
        try:
            with self._lock:
                return self.zoom.handle(root).strip()
        except (KeyError, ValueError, OSError) as e:
            self._log("family: handle of %s not made: %r" % (root, e))
            return ""

    def _child_car(self, child_session_id):
        """The dial family: the child's rows from state.db to its own store and a CAR (family.py). The call waits
        for it, so the CAR exists when the delegate_task call returns. A failure goes to provider.log: the child's
        work is still in state.db, and a delegation never fails because of it."""
        try:
            from .family import export_child
            got = export_child(os.path.join(self._hermes_home(), "state.db"), self.root, child_session_id)
        except Exception as e:
            self._log("family: child %s: %s: %s" % (child_session_id, type(e).__name__, e))
            return None
        if got is None:
            self._log("family: child %s: no rows to keep in state.db" % child_session_id)
        return got

    # -- summaries (one background thread; the model runs outside the lock) ---------------
    def _queue_summaries(self, crumb_ids):
        if not self.summarizer or not self.writable:
            return
        with self._lock:
            if self._jobs is None:
                self._jobs = queue.Queue()
                self._worker = _spawn(self._work, "breadcrumbs-summaries")
                self._worker.start()
            for cid in crumb_ids:
                if cid not in self._queued and not self.index.summary_of(cid):
                    self._queued.add(cid)
                    self._jobs.put(cid)

    def _work(self):
        while True:
            cid = self._jobs.get()
            try:
                if cid is None or self._stop:
                    return
                self._summarize_one(cid)
            except Exception as e:  # a failed summary must never break the agent
                self._log("summary %s failed: %r" % (cid, e))
            finally:
                self._queued.discard(cid)
                self._jobs.task_done()

    def _summarize_one(self, cid):
        runner, max_input = self.summarizer, MAX_INPUT
        for _ in range(TRIES):
            with self._lock:
                if self.store is None or runner is None:
                    return
                prompt = plan(self.store, self.index, cid, runner, max_input)
            if prompt is None:
                return
            try:
                out = runner(prompt)  # seconds of model time: no lock held
            except PromptTooLong as err:
                max_input = shrink(max_input, err)
                continue
            break
        else:
            return
        with self._lock:
            if self.store is None:
                return
            sid = store_summary(self.store, self.index, cid, runner, prompt, out)
            if sid:
                commit(self.store, self.refs, "summaries", [sid], "summary of " + cid)
                self.store.flush()
                self.index.commit()
                self.zoom.reset()

    def flush_summaries(self, timeout=None):
        """Wait until every queued summary is done (tests, CLI). True if the queue is empty."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._jobs is not None and self._jobs.unfinished_tasks:
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.01)
        return True

    def _log(self, msg, root=None):
        root = root or getattr(self, "root", None)
        if not root:
            return
        try:
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "provider.log"), "a", encoding="utf-8") as f:
                f.write("%s pid=%d %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), os.getpid(), msg))
        except OSError:
            pass

    def shutdown(self):
        if self.slot is not None:
            self.slot.call("shutdown")
            self.slot = None
        if self.hive is not None:
            self.hive.close()
            self.hive = None
        if self._jobs is not None:
            self._stop = True
            self._jobs.put(None)
            self._worker.join(timeout=15)
        with self._lock:
            if self.store is not None:
                self.index.close()
                self.store.close()
                self.store = None
