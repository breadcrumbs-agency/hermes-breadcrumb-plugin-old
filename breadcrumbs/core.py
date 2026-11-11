"""core: the one Python core that every harness reaches. It holds the stores open and answers six read-only verbs.

    find     search the stores              open    the exact text of a crumb (level 1)
    zoom     a crumb at a level 0..1        trail   the zoom stack of a store
    stores   the stores that this core sees health  the core's own state, and the calls of each harness

Three thin servers carry the same verb table, and none of them has logic of its own:
    mcp.py   MCP over stdio            (Claude Code, Codex, OpenCode and Hermes start it as a child)
    sock.py  one JSON line each way over a Unix socket or loopback TCP (the framing of hive.py)
    a2a.py   A2A over HTTP on loopback (agent card and message/send)

Stores: "default" is ~/.hermes/breadcrumbs, each profile is "<name>" (~/.hermes/profiles/<name>/breadcrumbs), and
"local" is --home or $BREADCRUMBS_HOME when it is neither. A verb takes `store`: a name, or "*" for all. With no
store, find, open and zoom look in all of them (a crumb id is a content hash, so the same id names the same text in
every store), and trail takes "local" when it is there, else "default". Each View stays open (resident) and is
opened again when index.db or blobs.pack is replaced. Nothing here writes a crumb.

Harnesses: a client names itself (MCP clientInfo.name, the A2A User-Agent or X-Harness header, the "harness" field of
a socket request). identify() maps the name to one key of HARNESSES. The core only counts calls per harness (health)
and prints each harness's setup command (`core setup NAME`); the verbs behave the same for all.
"""
import json
import os
import secrets
import shlex
import sys
import threading
import time

VERSION = "0.1.0"
MAX_CHARS = 200_000  # the longest text that one call returns; a longer text is cut and says so
LEVEL = 1.0  # `open` is zoom at this level


class CoreError(Exception):
    """code: unknown_verb, bad_params, not_found or no_store."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


# -- the harnesses -----------------------------------------------------------------------------------------
def _json(obj):
    return json.dumps(obj, indent=2)


HARNESSES = {
    "hermes": {
        "label": "Hermes", "match": ("hermes",),
        "setup": lambda cmd: "# ~/.hermes/config.yaml\nmcp_servers:\n  breadcrumbs:\n    command: %s\n    args: %s" % (
            cmd[0], json.dumps(cmd[1:]))},
    "claude-code": {
        "label": "Claude Code", "match": ("claude",),
        "setup": lambda cmd: "claude mcp add breadcrumbs -- " + shlex.join(cmd)},
    "codex": {
        "label": "Codex", "match": ("codex",),
        "setup": lambda cmd: "codex mcp add breadcrumbs -- " + shlex.join(cmd)},
    "opencode": {
        "label": "OpenCode", "match": ("opencode",),
        "setup": lambda cmd: "# opencode.json\n" + _json({"mcp": {"breadcrumbs": {
            "type": "local", "command": cmd, "enabled": True}}})},
}
UNKNOWN = "unknown"


def identify(name):
    """The key of HARNESSES that a client's self-name matches, else "unknown"."""
    low = (name or "").lower()
    for key, h in HARNESSES.items():
        if any(m in low for m in h["match"]):
            return key
    return UNKNOWN


def server_command(kind="mcp", env=None):
    """The command that starts a server of this core, found by Python wherever the harness starts it."""
    from .launch import REPO
    paths = [REPO] + [p for p in (env or os.environ).get("PYTHONPATH", "").split(os.pathsep) if p and p != REPO]
    return ["env", "PYTHONPATH=" + os.pathsep.join(paths), sys.executable, "-m", "breadcrumbs", "core", kind]


# -- the stores --------------------------------------------------------------------------------------------
class Stores:
    """Which stores exist. Names are read at each call: a profile made later shows up."""

    def __init__(self, local=None, root=None):
        self.root = os.path.abspath(os.path.expanduser(root or "~/.hermes"))
        self.local = os.path.abspath(os.path.expanduser(local)) if local else None

    @property
    def base(self):
        return os.path.join(self.root, "breadcrumbs")

    def names(self):
        """{name: folder}, in the order that the verbs look: local, default, then the profiles."""
        out = {}
        if self.local and os.path.realpath(self.local) != os.path.realpath(self.base):
            out["local"] = self.local
        out["default"] = self.base
        profiles = os.path.join(self.root, "profiles")
        try:
            for name in sorted(os.listdir(profiles)):
                home = os.path.join(profiles, name, "breadcrumbs")
                if name not in out and os.path.isdir(home):
                    out[name] = home
        except OSError:
            pass
        return out

    def present(self):
        return {n: h for n, h in self.names().items() if os.path.isfile(os.path.join(h, "index.db"))}


def _ident(home):
    ids = []
    for rel in ("index.db", os.path.join("store", "blobs.pack")):
        try:
            st = os.stat(os.path.join(home, rel))
            ids.append((st.st_ino, st.st_dev))
        except OSError:
            ids.append(None)
    return tuple(ids)


def token(path):
    """The access token of the TCP and A2A servers: random, kept in a file with mode 600, read at each start."""
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


# -- the verbs ---------------------------------------------------------------------------------------------
STORE = {"type": "string", "description": 'A store name, or "*" for all (see `stores`).'}
VERBS = {
    "find": {"doc": "Search the stores. Hits are grouped by session, best first.",
             "props": {"q": {"type": "string", "description": "The words to look for."},
                       "k": {"type": "integer", "description": "How many hits at most (1..50, default 8)."},
                       "store": STORE}, "required": ("q",)},
    "open": {"doc": "The exact text of a crumb.",
             "props": {"id": {"type": "string", "description": "A crumb id or ref from a find."}, "store": STORE},
             "required": ("id",)},
    "zoom": {"doc": "A crumb at a zoom level: 0 is the shortest, 1 is the whole text.",
             "props": {"id": {"type": "string", "description": "A crumb id or ref from a find."},
                       "level": {"type": "number", "description": "0..1 (default 0.25)."}, "store": STORE},
             "required": ("id",)},
    "trail": {"doc": "The zoom stack of a store: the crumbs that were opened last.",
              "props": {"store": STORE}, "required": ()},
    "stores": {"doc": "The stores that this core sees, and whether each one has data.", "props": {}, "required": ()},
    "health": {"doc": "The core's version, its stores, and the number of calls of each harness.", "props": {},
               "required": ()},
}
TYPES = {"string": str, "integer": int, "number": (int, float)}


def tool_schema(verb):
    v = VERBS[verb]
    return {"type": "object", "properties": v["props"], "required": list(v["required"]), "additionalProperties": False}


class Core:
    def __init__(self, stores=None):
        self.stores = stores or Stores()
        self.started = time.time()
        self.calls = {}
        self._views = {}  # store name -> (View, identity of index.db and blobs.pack)
        self._lock = threading.Lock()

    # -- one resident View for each store
    def view(self, name):
        from .view import View
        homes = self.stores.present()
        if name not in homes:
            raise CoreError("no_store", "no store %r with data here" % name)
        home, ident = homes[name], _ident(homes[name])
        with self._lock:
            held = self._views.get(name)
            if held and held[1] == ident:
                return held[0]
            if held:
                held[0].close()
            v = View(home)
            self._views[name] = (v, ident)
            return v

    def close(self):
        with self._lock:
            for v, _ in self._views.values():
                v.close()
            self._views.clear()

    def pick(self, params, everywhere=True):
        """The store names that a call looks in, in order."""
        want = params.get("store")
        present = list(self.stores.present())
        if want is None:
            return present if everywhere else present[:1]
        if want == "*":
            return present
        if want not in present:
            raise CoreError("no_store", "no store %r with data here (stores: %s)" % (want, ", ".join(present) or "none"))
        return [want]

    # -- the call
    def call(self, verb, params=None, harness=None):
        """The result of a verb: a JSON-encodable dict. A refusal is a CoreError."""
        if verb not in VERBS:
            raise CoreError("unknown_verb", "unknown verb %r (verbs: %s)" % (verb, ", ".join(VERBS)))
        params = {} if params is None else params
        if not isinstance(params, dict):
            raise CoreError("bad_params", "params must be an object")
        spec = VERBS[verb]
        for key, val in params.items():
            if key not in spec["props"]:
                raise CoreError("bad_params", "%s has no parameter %r" % (verb, key))
            want = TYPES[spec["props"][key]["type"]]
            if isinstance(val, bool) or not isinstance(val, want):
                raise CoreError("bad_params", "%s.%s must be a %s" % (verb, key, spec["props"][key]["type"]))
        for key in spec["required"]:
            if key not in params:
                raise CoreError("bad_params", "%s needs %r" % (verb, key))
        key = harness if harness in HARNESSES else UNKNOWN
        with self._lock:
            self.calls[key] = self.calls.get(key, 0) + 1
        return getattr(self, "do_" + verb)(**params)

    def do_find(self, q, k=8, store=None):
        k = max(1, min(50, k))
        groups = []
        for name in self.pick({"store": store}):
            for g in self.view(name).find(q, k):
                groups.append(dict(g, store=name))
        groups.sort(key=lambda g: -max(h["score"] for h in g["hits"]))
        return {"q": q, "groups": groups[:k]}

    def text(self, ref, level, store):
        for name in self.pick({"store": store}):
            try:
                got = self.view(name).crumb(ref, level)
            except (KeyError, ValueError):
                continue
            text, cut = got["text"], False
            if len(text) > MAX_CHARS:
                text, cut = text[:MAX_CHARS], True
            return {"id": ref, "level": level, "store": name, "tokens": got["tokens"], "text": text, "truncated": cut}
        raise CoreError("not_found", "no crumb %r in the stores that were looked in" % ref)

    def do_open(self, id, store=None):
        return self.text(id, LEVEL, store)

    def do_zoom(self, id, level=0.25, store=None):
        return self.text(id, min(1.0, max(0.0, float(level))), store)

    def do_trail(self, store=None):
        present = list(self.stores.present())
        name = store or ("local" if "local" in present else "default")
        if name == "*" or name not in present:
            raise CoreError("no_store", "trail needs one store with data (stores: %s)" % (", ".join(present) or "none"))
        return dict(self.view(name).trail(), store=name)

    def do_stores(self):
        present = self.stores.present()
        return {"stores": [{"name": n, "home": h, "present": n in present} for n, h in self.stores.names().items()]}

    def do_health(self):
        with self._lock:
            calls = dict(self.calls)
        return {"ok": True, "version": VERSION, "pid": os.getpid(), "uptime": round(time.time() - self.started, 1),
                "stores": list(self.stores.present()), "open": sorted(self._views), "calls": calls}
