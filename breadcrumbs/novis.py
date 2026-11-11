"""no-vis: the sessions and profiles that opt out of visualization.

The tags are one file for the machine: <Hermes root>/breadcrumbs/no-vis.json, {"profiles": [name], "sessions": [id]}.

- A tagged profile gets no visualization: its chats open no explorer at start and get no visualizer NOTE, /viz opens
  nothing, `serve`, `viz`, `open`, `term` and `spacegraph` refuse its store, and the desktop page neither lists it nor
  reads it.
- A tagged session is left out of every view that draws (the explorer page, term, spacegraph, the desktop page): the
  map, the calendar, Measures, Size, the Gantt rows of other chats, search, the trail and its crumbs. A request for it
  by id is refused as unknown. Its own chat (a resumed one) gets no visualization either.
- `bc`, `find`, `vault`, `core` and `hive` do not draw: they read every session as before.

A file that cannot be read hides everything (the tags are privacy). Only the private command writes the file. It is not
in `help`:

    python -m breadcrumbs no-vis                  the tagged profiles and sessions
    python -m breadcrumbs no-vis find [TEXT]      the profiles and sessions whose name, id or title holds TEXT
    python -m breadcrumbs no-vis add NAME|ID...   tag profiles (by name) and sessions (by id)
    python -m breadcrumbs no-vis rm NAME|ID...    untag them

It reads each profile's state.db read-only (the id, the start time and the title of each session).
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time

FILE = "no-vis.json"
KEYS = ("profiles", "sessions")
FIND_MAX = 50  # the sessions that find prints, newest first
TITLE_MAX = 60  # the characters of a title in a line


def _root(hermes=None):
    if hermes:
        return str(hermes)
    from .island import hermes_root
    return hermes_root()


def path(hermes=None):
    """The file of the tags."""
    return os.path.join(_root(hermes), "breadcrumbs", FILE)


def load(hermes=None):
    """The tags, {"profiles": [name], "sessions": [id]}, sorted; both empty when there is no file. A file that is not
    that shape raises ValueError; a file that cannot be read raises OSError."""
    where = path(hermes)
    try:
        with open(where, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {k: [] for k in KEYS}
    out = {}
    for k in KEYS:
        v = data.get(k, []) if isinstance(data, dict) else None
        if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ValueError("%s: %r is not a list of names" % (where, k))
        out[k] = sorted(set(v))
    return out


def save(tags, hermes=None):
    """Write the tags (atomic replace: a reader never sees half a file)."""
    where = path(hermes)
    os.makedirs(os.path.dirname(where), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (where, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: sorted(set(tags.get(k, []))) for k in KEYS}, f, indent=1)
        f.write("\n")
    os.replace(tmp, where)


def profile_at(home, hermes=None):
    """The name of the profile whose Hermes home is `home` ("default" for the Hermes root), or None for another folder
    (a demo store, an A/B arm)."""
    if not home:
        return None
    root, home = os.path.realpath(_root(hermes)), os.path.realpath(str(home))
    if home == root:
        return "default"
    return os.path.basename(home) if os.path.dirname(home) == os.path.join(root, "profiles") else None


class Hide:
    """hide(sid) is True when the session is tagged, or when the profile at Hermes home `home` is (then for every sid,
    and for "" too: a crumb of no session). hide(prefix, True) also matches a tagged id that starts with prefix (ace.log
    keeps the first 24 characters). The views that draw take one (view.View(hide=...)). It reads the file again when the
    file changes, so a running server follows the command."""

    def __init__(self, home=None, hermes=None):
        self.hermes = _root(hermes)
        self.profile = profile_at(home, self.hermes)
        self.file = path(self.hermes)
        self.error = ""
        self._seen, self._tags, self._lock = (), (False, frozenset()), threading.Lock()

    def tags(self):
        """(the profile is tagged, the tagged session ids), as the file holds them now."""
        try:
            st = os.stat(self.file)
            seen = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            seen = None
        except OSError as e:
            seen = str(e)
        with self._lock:
            if seen != self._seen:
                try:
                    t = load(self.hermes)
                    self._tags, self.error = (self.profile in t["profiles"], frozenset(t["sessions"])), ""
                except (OSError, ValueError) as e:  # privacy: a file that cannot be read hides everything
                    self._tags, self.error = (True, frozenset()), "%s cannot be read: %s" % (self.file, e)
                self._seen = seen
            return self._tags

    def __call__(self, sid="", prefix=False):
        profile, sessions = self.tags()
        if profile:
            return True
        if not sid:
            return False
        return sid in sessions or (prefix and any(s.startswith(sid) for s in sessions))

    def why(self, sid=""):
        """Why this profile (and the chat sid) gets no visualization, or "" when it does."""
        profile, sessions = self.tags()
        if self.error:
            return self.error
        if profile:
            return "profile `%s` opted out of visualization (no-vis)" % self.profile
        return "this chat opted out of visualization (no-vis)" if sid and sid in sessions else ""


def blocked(home, sid="", hermes=None):
    """Why the chat sid of the profile at Hermes home `home` gets no visualization ("" when it does)."""
    return Hide(home, hermes).why(sid)


def sessions(hermes=None):
    """Every session of every profile's state.db (read-only): [(id, profile, started_at, title)], newest first."""
    from . import logview
    from .island import profiles

    def run(conn):
        have = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "id" not in have:
            return []
        cols = ["id"] + [c if c in have else "NULL" for c in ("started_at", "title")]
        return conn.execute("SELECT %s FROM sessions" % ", ".join(cols)).fetchall()

    out = []
    for name, home in profiles(_root(hermes)):
        try:
            rows = logview.read(os.path.join(home, "state.db"), run)
        except (OSError, sqlite3.Error):
            continue
        out += [(sid, name, started or 0, title or "") for sid, started, title in rows]
    out.sort(key=lambda r: (-r[2], r[0]))
    return out


def _in_store(sid, hermes):
    """The profile whose breadcrumbs store holds session sid (a session that no state.db lists), or None."""
    from .hermes import _ref_name
    from .island import profiles
    for name, home in profiles(hermes):
        if os.path.exists(os.path.join(home, "breadcrumbs", "refs", _ref_name("sess-", sid))):
            return name
    return None


def _line(tagged, kind, name, row=None):
    out = "[%s] %-7s  %s" % ("x" if tagged else " ", kind, name)
    if row:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row[2])) if row[2] else "-"
        title = re.sub(r"\s+", " ", row[3]).strip()
        title = title[:TITLE_MAX - 1] + "\u2026" if len(title) > TITLE_MAX else title
        out += "  %s  %s  %s" % (row[1], when, title)
    return out.rstrip()


def _command(action="list", names=(), hermes=None, out=None):
    """`no-vis [list | find [TEXT] | add NAME|ID... | rm NAME|ID...]`: the private command, the only writer of the file.
    add refuses a name that is no profile and no session (of a state.db or a store), and rm one that is not tagged:
    then nothing changes. Returns the exit code."""
    out, hermes = out or sys.stdout, _root(hermes)
    try:
        tags = load(hermes)
    except (OSError, ValueError) as e:
        print("breadcrumbs no-vis: %s (fix the file or delete it)" % e, file=sys.stderr)
        return 1
    from .island import profiles
    rows, names_ = sessions(hermes), [n for n, _ in profiles(hermes)]
    known = {r[0]: r for r in rows}
    if action == "find":
        text = " ".join(names).lower()
        found = [n for n in names_ if text in n.lower()]
        hits = [r for r in rows if text in r[0].lower() or text in r[3].lower()]
        for n in found:
            print(_line(n in tags["profiles"], "profile", n), file=out)
        for r in hits[:FIND_MAX]:
            print(_line(r[0] in tags["sessions"], "session", r[0], r), file=out)
        if len(hits) > FIND_MAX:
            print("(%d more sessions: give more TEXT)" % (len(hits) - FIND_MAX), file=out)
        if not found and not hits:
            print("no profile or session holds %r" % text, file=out)
        return 0
    if action in ("add", "rm"):
        if not names:
            print("usage: no-vis %s NAME|ID..." % action, file=sys.stderr)
            return 2
        if action == "add":
            bad = []
            for n in names:
                if n in names_:
                    tags["profiles"].append(n)
                elif n in known or _in_store(n, hermes):
                    tags["sessions"].append(n)
                else:
                    bad.append(n)
            if bad:
                print("breadcrumbs no-vis: no profile or session %s; nothing changed" % ", ".join(bad), file=sys.stderr)
                return 1
        else:
            bad = [n for n in names if n not in tags["profiles"] and n not in tags["sessions"]]
            if bad:
                print("breadcrumbs no-vis: %s not tagged; nothing changed" % ", ".join(bad), file=sys.stderr)
                return 1
            tags = {k: [x for x in v if x not in names] for k, v in tags.items()}
        save(tags, hermes)
        tags = load(hermes)
    print("no-vis: %s" % path(hermes), file=out)
    for n in tags["profiles"]:
        print(_line(True, "profile", n), file=out)
    for sid in tags["sessions"]:
        print(_line(True, "session", sid, known.get(sid)), file=out)
    if not tags["profiles"] and not tags["sessions"]:
        print("nothing is tagged", file=out)
    return 0
