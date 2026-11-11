"""Views of a store: the explorer's data, the trail, and a read-only server on 127.0.0.1.

The page (view.html) lays crumbs out as SpaceGraph does: Graph2D nodes sized by priority, the
Force2D layout, and the Zoomed camera, whose zoom stack is the trail. This module gives
the page its data. It never writes to the store or the index. The API needs the token in
<root>/view.token, so another web page cannot read the memory through the loopback port.
"""

import bisect
import datetime
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from array import array
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import dials, logview
from .crumbs import Crumb, is_crumb
from .history import Refs
from .index import Index
from .store import Store
from .ingest import leaves
from .lines import decode_spine
from .zoom import CUED, GAP, Zoom, parse_ref

STACK_MAX = 8  # SpaceGraph's Zoomed.ZOOM_STACK_MAX
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "view.html")
HEAT_HALF = 86400.0  # seconds: the glow of a crumb the model opened halves each day
ACE_LOG = ("ace.log.1", "ace.log")  # oldest first
ACE_KV = re.compile(r"(\w+)=(\S+)")
GEAR_KEYS = ("think", "calls", "work", "room")  # the whole numbers that the gearbox adds to a line (gear is a word)
CUT_EDGES = (0.05, 0.10, 0.20, 0.40)  # a fold cuts less than 5%, 5 to 10%, 10 to 20%, 20 to 40%, 40% or more
CANDLES = 40  # about this many candles in a chart
ADDING = ("recall", "find", "open")  # the context events that put memory text into the context: the turn budget counts them


class Trail:
    """The zoom stack of one session, as in SpaceGraph's Zoomed: the top crumb again changes only its
    level; a crumb lower in the stack goes back to it; a new crumb is pushed, and past STACK_MAX the
    oldest leaves (the status line shows one gap mark for them)."""

    def __init__(self, stack=(), dropped=0, session=""):
        self.stack, self.dropped, self.session = [dict(e) for e in stack], dropped, session

    def visit(self, ref, level, label=""):
        at = [e["id"] for e in self.stack]
        if ref in at:
            del self.stack[at.index(ref) + 1:]
            self.stack[-1]["level"] = level
            if label:
                self.stack[-1]["label"] = label
            return
        self.stack.append({"id": ref, "level": level, "label": label or ref})
        if len(self.stack) > STACK_MAX:
            del self.stack[0]
            self.dropped += 1

    def line(self):
        return " \u203a ".join((["\u2026"] if self.dropped else []) + [e["label"] for e in self.stack])

    def to_dict(self):
        return {"session": self.session, "stack": self.stack, "dropped": self.dropped, "line": self.line()}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("stack", ()), d.get("dropped", 0), d.get("session", ""))


def label(zoom, ref):
    """A short trail name: the kind, then three cue words (or the title)."""
    head = zoom.handle(ref).strip()
    if head.startswith(GAP):
        return "text " + ref.partition(":")[2]
    kind = (head.split(None, 3) + ["", "", "?"])[2]
    cues = head.partition(" | ")[2].split()[:3]
    title = re.search(r'"(.*?)"', head)
    return kind + " " + (" ".join(cues) if cues else (title.group(1)[:28] if title else ref))


def token(root):
    """The access token of the views (random, kept in <root>/view.token with mode 600)."""
    path = os.path.join(root, "view.token")
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(16)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def ace_requests(root):
    """Every request in <root>/ace.log.1 and ace.log, oldest first, as {sess, ts, act, raw, sent, count, base,
    crumbs, lay, folds, target}. raw is the request before ACE ("in"), sent the request after it ("out"), both
    in tokens by ACE's estimate; count is the model's own count of the request before (0: none yet). A line that
    does not parse, and a line with no line end, is skipped. Nothing is written."""
    out = []
    for name in ACE_LOG:
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")[:-1]
        except OSError:
            continue
        for line in lines:
            kv = dict(ACE_KV.findall(line[19:]))
            try:
                ts = time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S"))
                row = {"sess": kv["sess"], "ts": ts, "act": kv["act"], "raw": int(kv["in"]), "sent": int(kv["out"]),
                       "count": int(kv.get("last", 0)), "base": None if kv.get("base", "None") == "None" else int(kv["base"]),
                       "crumbs": int(kv.get("layer", 0)), "lay": int(kv["lay"]) if "lay" in kv else None,
                       "folds": int(kv.get("folds", 0)), "target": int(kv["target"]) if "target" in kv else None,
                       "gear": kv.get("gear"), **{k: int(kv[k]) if k in kv else None for k in GEAR_KEYS}}
            except (KeyError, ValueError, OverflowError):
                continue
            out.append(row)
    return out


def ace_series(rows):
    """The requests of one session, oldest first, with what a chart needs. drop is the share of the request before
    that this request cut (0 when it grew); seen is the model's own count of what this request sent (the next
    request carries it as last; None until then)."""
    pts = []
    for i, r in enumerate(rows):
        prev = rows[i - 1]["sent"] if i else 0
        nxt = rows[i + 1]["count"] if i + 1 < len(rows) else 0
        pts.append({**{k: r[k] for k in ("ts", "act", "raw", "sent", "base", "crumbs", "lay", "folds", "target", "gear", *GEAR_KEYS)},
                    "i": i, "seen": nxt or None,
                    "drop": round(max(0.0, 1 - r["sent"] / prev), 4) if prev else 0.0})
    return pts


def ace_candles(pts, per=0):
    """Open, high, low and close of the sent size over runs of `per` requests (default: about CANDLES candles in
    all), with the folds inside each run."""
    per = per if per > 0 else max(1, -(-len(pts) // CANDLES))
    out = []
    for a in range(0, len(pts), per):
        run = pts[a:a + per]
        sent = [p["sent"] for p in run]
        out.append({"i0": run[0]["i"], "i1": run[-1]["i"], "ts0": run[0]["ts"], "ts1": run[-1]["ts"], "n": len(run),
                    "open": sent[0], "high": max(sent), "low": min(sent), "close": sent[-1],
                    "folds": sum(1 for p in run if p["act"] in ("fold", "ratchet"))})
    return out


def ace_mix(pts):
    """What the last request held, in tokens: base (the fixed part: system prompt and tools), layer (the crumbs),
    exact (the turns word for word) and held (the rest of the chat, one crumb call away). None for an empty
    session. A part that the log does not know (an older line has no base or no lay) counts 0 and is named in
    unknown."""
    if not pts:
        return None
    p = pts[-1]
    base, lay = p["base"] or 0, p["lay"] or 0
    unknown = [k for k in ("base", "lay") if p[k] is None]
    return {"i": p["i"], "base": base, "layer": lay, "exact": max(0, p["sent"] - base - lay), "sent": p["sent"],
            "raw": p["raw"], "held": max(0, p["raw"] - p["sent"]), "unknown": unknown}


def ace_totals(by):
    """The aggregate of all sessions in the log: the requests by act, the folds' cut sizes in bins (CUT_EDGES), and
    one summary row per session, the newest first. The first request of a session is no cut."""
    acts, cuts, rows = {}, [], []
    for sess, pts in by.items():
        mine = {}
        for p in pts:
            mine[p["act"]] = mine.get(p["act"], 0) + 1
            acts[p["act"]] = acts.get(p["act"], 0) + 1
            if p["act"] == "fold" and p["i"]:
                cuts.append(p["drop"])
        rows.append({"sess": sess, "n": len(pts), "t0": pts[0]["ts"], "t1": pts[-1]["ts"], "acts": mine,
                     "folds": mine.get("fold", 0) + mine.get("ratchet", 0), "max_sent": max(p["sent"] for p in pts),
                     "target": next((p["target"] for p in reversed(pts) if p["target"]), None)})
    rows.sort(key=lambda r: -r["t1"])
    bins = [0] * (len(CUT_EDGES) + 1)
    for c in cuts:
        bins[sum(1 for e in CUT_EDGES if c >= e)] += 1
    ordered = sorted(cuts)
    keeps = acts.get("keep", 0)
    return {"requests": sum(acts.values()), "sessions": len(rows), "acts": acts, "folds": len(cuts),
            "edges": list(CUT_EDGES), "bins": bins, "median_cut": ordered[len(ordered) // 2] if ordered else None,
            "cache_kept": round(keeps / (keeps + acts.get("fold", 0)), 4) if keeps + acts.get("fold", 0) else None,
            "rows": rows}


# -- measures: numbers with four keys (scope, context, family, inclusion) ---------------------------------------
UNITS = ("time", "turn", "request", "event")  # the units of the context axis
ROWS_MAX, COLS_MAX, LINES_MAX, POINTS_MAX, WALK_NODES, LABELS_MAX, NUL_MAX = 300, 400, 10, 800, 200000, 24, 4000000
RCPT_IDS = 20  # a cell that sums receipts names this many of them at most in its source
USAGE_LOG = ("usage.log.1", "usage.log")  # oldest first; breadcrumbs/usage_log.py writes them (one line per provider request)
USAGE_NUM = ("in", "cr", "cw", "out", "rsn", "prompt", "est", "msgs", "tools", "n")
AUTO_HOURS = 72  # the time axis has hour buckets when the data spans this many hours or fewer, else day buckets
ACE_PREFIX = 24  # ace.log holds this many characters of a session id (breadcrumbs.ace.LOG_SESS)
# The only columns that measures() reads from state.db. Never title, display_name, system_prompt,
# last_activity_description, origin_json, handoff_*, and never the messages table.
SESSION_COLS = ("id", "parent_session_id", "created_source", "started_at", "ended_at", "last_activity_at", "message_count",
                "tool_call_count", "api_call_count", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "estimated_cost_usd", "actual_cost_usd", "cwd",
                "git_repo_root", "git_branch")
MODEL_USAGE_COLS = ("session_id", "model", "task", "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
                    "first_seen", "last_seen")
# The only columns that calendar() reads from state.db: ids, the channel (how the chat was made) and three times.
CAL_COLS = ("id", "parent_session_id", "created_source", "started_at", "ended_at", "last_activity_at")
CAL_MAX = 1000  # sessions in one answer of calendar()


def _measure(key, label, unit, group, src, units, agg="sum", keyed=False, walks=False):
    return {"key": key, "label": label, "unit": unit, "agg": agg, "group": group, "src": src, "units": list(units),
            "keyed": keyed, "walks": walks}


# group: where the number is recorded. units: the context units in which it has a position (a number with no position
# in a unit has no cell there: "no data", never a guess). walks: the number is read from the crumb tree of every chat (the
# store), which costs a read of every crumb and every spine of the store (5 s for a store of 23 MB with Hermes's Python); the others
# come from state.db, the logs and the receipts, and cost a read of a few files. measures() with no m leaves out the measures that
# walk; m="all" or their names asks for them.
MEASURES = [
    _measure("in_tok", "input tokens", "tokens", "db", "state.db sessions.input_tokens", ("time",)),
    _measure("out_tok", "output tokens", "tokens", "db", "state.db sessions.output_tokens", ("time",)),
    _measure("cache_read", "cache read tokens", "tokens", "db", "state.db sessions.cache_read_tokens", ("time",)),
    _measure("cache_write", "cache write tokens", "tokens", "db", "state.db sessions.cache_write_tokens", ("time",)),
    _measure("reason_tok", "reasoning tokens", "tokens", "db", "state.db sessions.reasoning_tokens", ("time",)),
    _measure("api_calls", "api calls", "count", "db", "state.db sessions.api_call_count", ("time",)),
    _measure("tool_calls", "tool calls", "count", "db", "state.db sessions.tool_call_count", ("time",)),
    _measure("messages", "messages", "count", "db", "state.db sessions.message_count", ("time",)),
    _measure("cost_usd", "cost", "usd", "cost", "state.db session_model_usage", ("time",), keyed=True),
    _measure("u_prompt", "prompt tokens", "tokens", "usage", "usage.log prompt=", ("time", "request"), keyed=True),
    _measure("u_in", "input tokens (request)", "tokens", "usage", "usage.log in=", ("time", "request"), keyed=True),
    _measure("u_cr", "cache read tokens (request)", "tokens", "usage", "usage.log cr=", ("time", "request"), keyed=True),
    _measure("u_cw", "cache write tokens (request)", "tokens", "usage", "usage.log cw=", ("time", "request"), keyed=True),
    _measure("u_out", "output tokens (request)", "tokens", "usage", "usage.log out=", ("time", "request"), keyed=True),
    _measure("u_rsn", "reasoning tokens (request)", "tokens", "usage", "usage.log rsn=", ("time", "request"), keyed=True),
    _measure("ace_raw", "raw (before ACE)", "tokens", "ace", "ace.log in=", ("time", "request")),
    _measure("ace_sent", "sent (after ACE)", "tokens", "ace", "ace.log out=", ("time", "request")),
    _measure("ace_base", "base (prompt, tools)", "tokens", "ace", "ace.log base=", ("time", "request")),
    _measure("ace_lay", "layer (crumbs)", "tokens", "ace", "ace.log lay=", ("time", "request")),
    _measure("ace_exact", "exact turns", "tokens", "ace", "ace.log out - base - lay", ("time", "request")),
    _measure("ace_held", "held back", "tokens", "ace", "ace.log in - out", ("time", "request")),
    _measure("ace_folds", "folds", "count", "ace", "ace.log act=fold", ("time", "request")),
    _measure("ace_target", "target", "tokens", "ace", "ace.log target=", ("time", "request"), agg="max"),
    _measure("ctx_bytes", "bytes put in the context", "bytes", "rcpt", "receipts (rcpt crumbs)", ("time", "turn", "event")),
    _measure("size", "crumb size", "bytes", "store", "the store: bytes of the level-1 text", ("time", "turn"), walks=True),
]
MEASURE = {m["key"]: m for m in MEASURES}
DB_COLUMN = {"in_tok": "input_tokens", "out_tok": "output_tokens", "cache_read": "cache_read_tokens",
             "cache_write": "cache_write_tokens", "reason_tok": "reasoning_tokens", "api_calls": "api_call_count",
             "tool_calls": "tool_call_count", "messages": "message_count"}
USAGE_FIELD = {"u_prompt": "prompt", "u_in": "in", "u_cr": "cr", "u_cw": "cw", "u_out": "out", "u_rsn": "rsn"}
GROUP_OF = {m["key"]: m["group"] for m in MEASURES}


def usage_requests(root):
    """Every request in <root>/usage.log.1 and usage.log, oldest first, as {sess, ts, turn, req, task, model, resp, prov,
    in, cr, cw, out, rsn, prompt, est, msgs, tools, n, dur, fin, line, file, no}. A line is
    "<local time> key=value ..." (breadcrumbs/usage_log.py): sess is the whole session id, task is main or the task of an
    auxiliary call, prompt is the size of the context that the provider counted, "-" is a value that is not known. line is
    the line itself (numbers and ids only). A line that does not parse, and a line with no line end, is skipped. Nothing
    is written."""
    out = []
    for name in USAGE_LOG:
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                lines = f.read().split("\n")[:-1]
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            line = line.rstrip("\r")
            kv = dict(ACE_KV.findall(line[19:]))
            try:
                row = {"ts": time.mktime(time.strptime(line[:19], "%Y-%m-%dT%H:%M:%S")), "sess": kv["sess"]}
                for k in USAGE_NUM:
                    row[k] = int(kv.get(k, 0))
                row["dur"] = float(kv.get("dur", 0) if kv.get("dur", "-") != "-" else 0)
            except (KeyError, ValueError, OverflowError):
                continue
            for k in ("turn", "req", "resp", "prov", "fin"):
                row[k] = None if kv.get(k, "-") == "-" else kv[k]
            row["task"] = kv.get("task") or "main"
            row["model"] = None if kv.get("model", "-") == "-" else kv["model"]
            row.update(line=line, file=name, no=no)
            out.append(row)
    return out


def read_state(path, connect=None):
    """The numbers of a profile's state.db, read-only (mode=ro, query_only, closed at once; see logview.read):
    ({sid: row of SESSION_COLS}, [row of MODEL_USAGE_COLS] or None when there is no session_model_usage table), or
    None when there is no state.db or no sessions table. Only the columns of SESSION_COLS and MODEL_USAGE_COLS that
    the file has are selected."""
    def run(conn):
        def cols(table):
            return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]

        have = cols("sessions")
        if "id" not in have:
            return None
        pick = [c for c in SESSION_COLS if c in have]
        sessions = {r[0]: dict(zip(pick, r)) for r in conn.execute("SELECT %s FROM sessions" % ", ".join(pick)).fetchall()}
        use, rows = cols("session_model_usage"), None
        if "session_id" in use:
            pick = [c for c in MODEL_USAGE_COLS if c in use]
            rows = [dict(zip(pick, r)) for r in
                    conn.execute("SELECT %s FROM session_model_usage" % ", ".join(pick)).fetchall()]
        return sessions, rows

    try:
        return logview.read(path, run, connect)
    except (OSError, sqlite3.Error):
        return None


def read_times(path, connect=None):
    """The times and ids of a profile's state.db sessions, read-only like read_state: {sid: row of CAL_COLS}, or None when
    there is no state.db or no sessions table. Only the columns of CAL_COLS that the file has are selected: no text, no
    messages, none of the numbers of read_state."""
    def run(conn):
        have = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "id" not in have:
            return None
        pick = [c for c in CAL_COLS if c in have]
        return {r[0]: dict(zip(pick, r)) for r in conn.execute("SELECT %s FROM sessions" % ", ".join(pick)).fetchall()}

    try:
        return logview.read(path, run, connect)
    except (OSError, sqlite3.Error):
        return None


def recorded_cost(row):
    """(usd, column) of a session_model_usage row: actual_cost_usd when it is set (above 0), else estimated_cost_usd.
    The usd is None when the table does not know the cost: a cost of 0 with no status, or with status unknown, is not
    a free request but an unpriced one."""
    actual, est = row.get("actual_cost_usd"), row.get("estimated_cost_usd")
    if actual:
        return actual, "actual_cost_usd"
    if est is None:
        return None, "estimated_cost_usd"
    if not est and row.get("cost_status") in (None, "", "unknown"):
        return None, "estimated_cost_usd"
    return est, "estimated_cost_usd"


def short_sid(sid):
    """A session id that a person can tell from the next one: the 8 characters after local-, the time and suffix of a
    dated id, else the first 12."""
    m = re.match(r"^\d{8}_(\d{6})_(\w+)$", sid)
    return sid[6:14] if sid.startswith("local-") else "%s_%s" % (m.group(1), m.group(2)[:6]) if m else sid[:12]


def _midnight(ts, days=0):
    """The local midnight that starts the day `days` after the day of ts (days below 0: before it). The day goes through the
    calendar and mktime gets a day that exists: CPython's mktime reads day 32 of a month as day 1 of the next one, but
    GraalPy's raises a Java DateTimeException that ends the interpreter."""
    d = time.localtime(ts)
    day = datetime.date(d.tm_year, d.tm_mon, d.tm_mday) + datetime.timedelta(days=days)
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def time_buckets(lo, hi):
    """The time axis for data from lo to hi: ("hour"|"day", [start of each bucket]). Contiguous, so the axis is linear in
    time. Hours when the data spans AUTO_HOURS hours or fewer, else local days."""
    if hi - lo <= AUTO_HOURS * 3600:
        a = int(lo // 3600 * 3600)
        return "hour", [float(a + 3600 * i) for i in range(int((hi - a) // 3600) + 1)]
    starts, t = [], _midnight(lo)
    while t <= hi:
        starts.append(t)
        t = _midnight(t, 1)
    return "day", starts


def place(ts, starts, end):
    """Where a time falls on an axis whose columns start at `starts` (the last one ends at `end`): a column number and a
    share of that column (column 2.5 is the middle of the third column). Before the first column: 0."""
    i = bisect.bisect_right(starts, ts) - 1
    if i < 0:
        return 0.0
    stop = starts[i + 1] if i + 1 < len(starts) else max(end, starts[i] + 1)
    return i + min(0.999, (ts - starts[i]) / (stop - starts[i]))


def request_slots(ace, usage):
    """The requests of one session on one axis. ace: its ace.log rows, usage: its usage.log rows of task main, both oldest
    first. An ace.log line opens a request; the first usage.log line after it (before the next ace.log line) closes it.
    A usage line that no ace line opened is a request of its own, and so is an ace line that no usage line followed.
    -> [{"ace": row|None, "usage": row|None, "ts"}]"""
    slots, open_slot = [], None
    for ts, kind, row in sorted([(r["ts"], 0, r) for r in ace] + [(r["ts"], 1, r) for r in usage], key=lambda e: e[:2]):
        if kind == 0:
            open_slot = {"ace": row, "usage": None, "ts": ts}
            slots.append(open_slot)
        elif open_slot is not None and open_slot["usage"] is None:
            open_slot["usage"] = row
        else:
            slots.append({"ace": None, "usage": row, "ts": ts})
            open_slot = None
    return slots


class _Cell:
    """One number of an answer and where it came from: value, how many records it sums, their first and last time."""
    __slots__ = ("v", "n", "t0", "t1", "src")

    def __init__(self, v, ts, src):
        self.v, self.n, self.t0, self.t1, self.src = v, 1, ts, ts, src

    def add(self, v, ts, agg):
        self.v = max(self.v, v) if agg == "max" else self.v + v
        self.n += 1
        if ts is not None:
            self.t0 = ts if self.t0 is None else min(self.t0, ts)
            self.t1 = ts if self.t1 is None else max(self.t1, ts)


def _put(acc, key, v, ts, src, agg="sum"):
    """acc[key] takes v (key = (context, measure, model, task)). A measure of agg max keeps the largest."""
    cell = acc.get(key)
    if cell is None:
        acc[key] = _Cell(v, ts, src)
    else:
        cell.add(v, ts, agg)


def _roll(into, other, agg_of):
    """into[key] takes the plain numbers of other ({key: number}): the sum, or the largest for a measure of agg max."""
    for key, v in other.items():
        have = into.get(key)
        into[key] = v if have is None else max(have, v) if agg_of[key[1]] == "max" else have + v


_NUL = re.compile(b"\x00")  # a NUL byte: _Sizer.nul_at looks for it in each block of the store


class _Sizer:
    """Zoom.size (the bytes of the level-1 expansion of a crumb) for the crumbs of one walk, without a read of every line of every
    text. Zoom.size reads each line to count its bytes and to see whether the text is binary (a NUL in a line: the text is then one
    line of words, see Zoom._doc_lines). The store's index already holds the length of every line, so this reads the spine of a
    text for the lengths, and each block of the store once for the NUL check (a block with no NUL has no line with one). The sizes
    are Zoom.size's to the byte (tests/test_view.py counts them again with a Zoom of its own). A line is not checked against its own
    CRC32C here (its block is): a size is not a text. What this cannot place (a part that is no crumb and no spine, a line the index
    does not hold, a Store without these arrays) it hands to Zoom.size. Every size is also put in zoom._full, where label() and
    handle() look for it."""

    def __init__(self, view, get):
        self.zoom, self.store, self.get, self.view = view.zoom, view.store, get, view
        self.memo, self.docs, self.nul, self.kids = {}, {}, view._nul, {}  # nul: shared by the walks of the View
        self.fast = all(hasattr(self.store, a) for a in ("_row", "_len", "_blk", "_off", "_block", "_read", "_pending"))

    def parts(self, ref, c):
        """Zoom.parts of a crumb, asked of the index once."""
        if ref not in self.kids:
            self.kids[ref] = self.zoom.parts(c, ref)
        return self.kids[ref]

    def nul_at(self, blk):
        """Where the NUL bytes of a block of the store are (an array of offsets, in order). Most blocks hold some, in the spines, so
        the question is asked of each line; a block is read once for all the walks of the View. The cache is cut when it holds more
        than NUL_MAX offsets (a store of binary texts has a NUL in every few bytes)."""
        pos = self.nul.get(blk)
        if pos is None:
            pos = array("I", [m.start() for m in _NUL.finditer(self.store._block(blk))])
            if self.view._nul_held + len(pos) > NUL_MAX:
                self.nul.clear()
                self.view._nul_held = 0
            self.view._nul_held += len(pos)
            self.nul[blk] = pos
        return pos

    def has_nul(self, p):
        """True when the line of row p of the store holds a NUL byte."""
        st = self.store
        if p in st._pending:
            return b"\x00" in st._read(p)
        pos = self.nul_at(st._blk[p])
        off = st._off[p]
        i = bisect.bisect_left(pos, off)
        return i < len(pos) and pos[i] < off + st._len[p]

    def doc(self, doc_id):
        """(bytes of all its lines, [bytes of each line], binary) of a text, or None when doc_id is not a spine or a line is not in the index."""
        if doc_id not in self.docs:
            st, rows = self.store, []
            blob, out = st.get(doc_id), None  # a text the store lacks raises KeyError, as it does in Zoom
            if blob[:4] == b"SP01":
                for crc, ordinal in decode_spine(blob):
                    p = st._row(crc, ordinal)
                    if p < 0:
                        break
                    rows.append(p)
                else:
                    lens = [st._len[p] for p in rows]
                    out = (sum(lens), lens, any(self.has_nul(p) for p in sorted(set(rows), key=st._blk.__getitem__)))
            self.docs[doc_id] = out
        return self.docs[doc_id]

    def range_size(self, ref):
        doc, a, b = parse_ref(ref)
        d = self.doc(doc)
        if d is None:
            return self.zoom.size(ref)
        total, lens, binary = d
        return sum(len(x) for x in [b"(binary, %d bytes: not shown)\n" % total][a:b]) if binary else sum(lens[a:b])

    def size(self, ref):
        n = self.memo.get(ref)
        if n is not None:
            return n
        if not self.fast:
            n = self.zoom.size(ref)
        elif ":" in ref:
            n = self.range_size(ref)
        else:
            c = self.get(ref)
            if c is None:
                n = self.zoom.size(ref)
            else:
                self.memo[ref] = 0  # while the parts are measured, as Zoom.size does
                n = sum(self.size(p) for p in self.parts(ref, c))
                n += len(self.zoom._head(ref, c, 0, False, (n + 3) // 4).encode("utf-8"))
        self.memo[ref] = n
        self.zoom._full.setdefault(ref, n)
        return n


class _Walk:
    """The crumb tree of one session, flat and in depth-first order, so a subtree is the slice [i, end[i]). A node is one
    place in the tree. selfb: the bytes that the crumb itself holds: its handle line and its own text, so a crumb's
    size (zoom.size) is selfb + the sizes of its kids exactly. ts and turn: where the node starts, that is the time of
    its first leaf and the user turn that holds it (a leaf is a crumb without kids; a leaf of role user opens a turn;
    the leaves before the first user turn are in turn 0, as View.context counts)."""

    def __init__(self, view, ref):
        cache = {}

        def get(cid):  # each crumb is decoded once, however often the tree holds it
            if cid not in cache:
                cache[cid] = view.crumb_or_none(cid)
            return cache[cid]

        size = _Sizer(view, get)
        parts = size.parts
        size = size.size
        self.refs, self.parent, self.kind, self.kids, self.selfb, self.title = [], [], [], [], [], []
        self.ts, self.turn, self.turns, self.first = [], [], [], {}
        stack, done, last_ts, count = [(ref, -1)], [], None, -1
        while stack:
            cid, up = stack.pop()
            i = len(self.refs)
            c = get(cid)
            kids = [p for p in (parts(cid, c) if c else []) if ":" not in p and get(p) is not None]
            self.refs.append(cid)
            self.parent.append(up)
            self.kind.append(c.kind if c else "text")
            self.title.append(c.title if c else "")
            self.kids.append([])
            self.selfb.append(size(cid) - sum(size(k) for k in kids))
            self.ts.append(None)
            self.turn.append(0)
            done.append(False)
            self.first.setdefault(cid, i)
            if up >= 0:
                self.kids[up].append(i)
            if not kids:  # a leaf: it fixes the start of every crumb above it that has no start yet
                meta = c.meta if c else {}
                if meta.get("role") == "user":
                    count += 1
                    self.turns.append((count, cid, meta.get("ts")))
                ts = meta.get("ts")
                last_ts = ts if ts is not None else last_ts
                j = i
                while j >= 0 and not done[j]:
                    self.ts[j], self.turn[j], done[j] = last_ts, max(count, 0), True
                    j = self.parent[j]
            stack.extend((k, i) for k in reversed(kids))
        n = len(self.refs)
        self.end = [1] * n
        for i in range(n - 1, 0, -1):
            self.end[self.parent[i]] += self.end[i]
        self.end = [i + s for i, s in enumerate(self.end)]


def _skey(sid):
    return "s:" + quote(sid, safe="")


def _short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _nums(cells):
    return {k: c.v for k, c in cells.items()}


def _put_rcpt(acc, key, v, ts, rid):
    """Like _put, for a number that a receipt gave: the cell keeps the ids of the receipts that it sums."""
    cell = acc.get(key)
    if cell is None:
        acc[key] = _Cell(v, ts, ["rcpt", [rid]])
    else:
        cell.add(v, ts, "sum")
        if rid not in cell.src[1] and len(cell.src[1]) < RCPT_IDS:
            cell.src[1].append(rid)


class _Measures:
    """One answer of View.measures. It reads state.db, ace.log, usage.log, the receipts and the store, and writes nothing.

    The four keys of a number, and the code that holds each:
      scope    S[sid]["scope"], scopes(): profile > codebase > branch of the session (collect, scope_of).
      context  self.ctx, axis(): the columns (time buckets, user turns, requests, context events).
      family   the rows: lineage() for sessions, expand() for the crumbs in a session; a fact names its row by key.
      family inclusion  fact_rows(): self is the row's own number, incl is the row plus every member that it includes."""

    def __init__(self, view, roots, unit, sid, want, codebase, branch, incl, lines, limit, names=False):
        self.v, self.roots, self.unit, self.focus, self.want = view, roots, unit, sid, want
        self.codebase, self.branch, self.lines_of_family, self.want_lines, self.limit = codebase, branch, incl, lines, limit
        self.names = names  # True: the label of a chat (and of a span) holds its cue words; False: a quick label, see quick()
        self.agg = {m["key"]: m["agg"] for m in MEASURES}
        self.capped = {"rows": False, "context": False, "points": False, "limit": limit, "total_rows": 0}
        self.models, self._cells, self._foreign, self._roots, self._crumbs = set(), {}, {}, {}, {}
        self.ctx, self.starts, self.end, self.kind, self.slots, self.anchors, self.slot_of = [], [], 0.0, "", [], [], {}

    # -- reading ---------------------------------------------------------------------------------------------
    def run(self):
        v = self.v
        state = read_state(os.path.join(os.path.dirname(os.path.abspath(v.root)), "state.db"), v.db_connect)
        self.db, models = state if state else (None, None)
        self.cost_by = {}
        for r in models or ():
            self.cost_by.setdefault(r.get("session_id"), []).append(r)
        self.ace_by, self.use_by = {}, {}
        for r in ace_requests(v.root):
            self.ace_by.setdefault(r["sess"], []).append(r)
        for r in usage_requests(v.root):
            self.use_by.setdefault(r["sess"], []).append(r)
        with v.lock:
            self.collect()
            self.lineage()
            self.axis()
            return self.answer()

    def crumb(self, ref):
        """The crumb with this id, or None when the store has none. It is decoded once for this answer."""
        if ref not in self._crumbs:
            self._crumbs[ref] = self.v.crumb_or_none(ref)
        return self._crumbs[ref]

    def label(self, ref):
        try:
            return label(self.v.zoom, ref)
        except (KeyError, ValueError, IndexError, OSError):
            return ref  # a ref that this store cannot show (another host's)

    def quick(self, kind, title, ref, t0=None, sid=""):
        """A label that costs no read. The cue words of label() need the whole text under a chat or a span (and the counts of the
        index), which is a read of every line of it, so the first answer does not wait for them: View.labels gives them later.
        A chat: its start time (its short id is a field of the row). A span: the title of its crumb, as label() does when a crumb
        has no cue words."""
        if kind == "sess":
            return "sess " + (time.strftime("%m-%d %H:%M", time.localtime(t0)) if t0 else short_sid(sid))
        inner = re.match(r'"(.*?)"', json.dumps(title[:40], ensure_ascii=False)) if title else None
        return kind + " " + (inner.group(1)[:28] if inner else ref)

    def edge_ts(self, ref, last):
        """The time of the first (or last) leaf of a crumb: the way down only follows one kid."""
        c = self.crumb(ref)
        while c is not None and c.kids:
            c = self.crumb(c.kids[-1 if last else 0])
        return c.meta.get("ts") if c is not None else None

    def scope_of(self, rec):
        """(codebase, branch) of a session: git_repo_root, else the codebase that holds its cwd (island.root_of), else none."""
        if rec is None:
            return "none", "none"
        repo, cwd = rec.get("git_repo_root") or "", rec.get("cwd") or ""
        if not repo and cwd and (os.path.isabs(cwd) or cwd.startswith("~")):
            if cwd not in self._roots:
                try:
                    from .island import root_of
                    self._roots[cwd] = root_of(cwd) or ""
                except Exception:  # a codebase that cannot be placed is none
                    self._roots[cwd] = ""
            repo = self._roots[cwd]
        return repo or "none", rec.get("git_branch") or "none"

    def collect(self):
        """The sessions: the rows of state.db and the sessions of the store, joined by session id."""
        v, S = self.v, {}
        self.S = S
        for sid, rec in (self.db or {}).items():
            if not v._hidden(sid):
                S[sid] = {"sid": sid, "rec": rec, "ref": None, "parent": rec.get("parent_session_id") or None}
        for name in v.refs.names():
            if name.startswith("sess-"):
                rid = v.refs.get(name)
                c = self.crumb(rid) if rid else None
                sid = c.meta.get("sess", "") if c is not None else ""
                if sid and not v._hidden(sid):
                    S.setdefault(sid, {"sid": sid, "rec": None, "ref": None, "parent": None})["ref"] = rid
        if (self.unit != "time" or self.focus) and self.focus not in S:
            raise KeyError(self.focus)
        self.children_all = {}
        for s in S.values():
            rec = s["rec"] or {}
            t0 = rec.get("started_at")
            ends = [x for x in (rec.get("ended_at"), rec.get("last_activity_at")) if x]
            t1 = max(ends) if ends else None
            if s["ref"]:
                t0 = self.edge_ts(s["ref"], False) if t0 is None else t0
                t1 = self.edge_ts(s["ref"], True) if t1 is None else t1
            for r in self.cost_by.get(s["sid"], ()):
                if r.get("last_seen") is not None:
                    t1 = max(t1 or 0, r["last_seen"])
            s["t0"], s["t1"] = t0, (t0 if t1 is None or (t0 is not None and t1 < t0) else t1)
            if self.db is None:  # no state.db: the store is the one scope
                s["cb"] = s["br"] = None
                s["scope"] = "profile"
            else:
                s["cb"], s["br"] = self.scope_of(s["rec"])
                s["scope"] = "br:%s|%s" % (s["cb"], s["br"])
            if s["parent"] and s["parent"] != s["sid"]:
                self.children_all.setdefault(s["parent"], []).append(s["sid"])
        home = os.path.dirname(os.path.abspath(v.root))
        try:
            from .island import profile_of
            self.profile = profile_of(home)
        except Exception:
            self.profile = os.path.basename(home) or "store"
        self.scopes = [{"key": "profile", "kind": "profile", "label": self.profile, "parent": None, "n": len(S)}]
        if self.db is not None:
            tree = {}
            for s in S.values():
                tree.setdefault(s["cb"], {}).setdefault(s["br"], 0)
                tree[s["cb"]][s["br"]] += 1
            for cb in sorted(tree, key=lambda x: (x == "none", x)):
                self.scopes.append({"key": "cb:" + cb, "kind": "codebase", "label": _short_path(cb), "parent": "profile",
                                    "n": sum(tree[cb].values()), "codebase": cb})
                for br in sorted(tree[cb], key=lambda x: (x == "none", x)):
                    self.scopes.append({"key": "br:%s|%s" % (cb, br), "kind": "branch", "parent": "cb:" + cb,
                                        "label": br if br != "none" else "no branch", "n": tree[cb][br], "codebase": cb,
                                        "branch": br})

    def lineage(self):
        """The rows of the top level: the sessions of the scope in a tree (a session under the one it continues, branches
        from, or was delegated by), the newest root first. With a unit of turn, request or event: the focus session."""
        S = self.S
        keep = [self.focus] if self.unit != "time" else \
            [sid for sid, s in S.items() if (not self.codebase or s["cb"] == self.codebase)
             and (not self.branch or s["br"] == self.branch)]
        ks = set(keep)
        self.kids, top = {sid: [] for sid in keep}, []
        for sid in keep:
            p = S[sid]["parent"]
            (self.kids[p] if p in ks and p != sid else top).append(sid)
        top.sort(key=lambda x: (-(S[x]["t0"] or 0), x))
        for lst in self.kids.values():
            lst.sort(key=lambda x: (S[x]["t0"] or 0, x))
        self.order, self.depth, seen = [], {}, set()

        def descend(root):
            stack = [(root, 0)]
            while stack:
                sid, d = stack.pop()
                if sid not in seen:
                    seen.add(sid)
                    self.order.append(sid)
                    self.depth[sid] = d
                    stack.extend((c, d + 1) for c in reversed(self.kids[sid]))

        for r in top:
            descend(r)
        for sid in sorted(keep, key=lambda x: (-(S[x]["t0"] or 0), x)):  # a cycle has no root: its newest session is one
            if sid not in seen:
                top.append(sid)
                descend(sid)
        self.top = top

    def events_of(self, sid):
        """The receipts of a session, oldest first: {i, id, ev, turn, ts, items: [(ref, bytes)]}."""
        from .hermes import _ref_name
        head = self.v.refs.get(_ref_name("rcpt-", sid))
        if not head:
            return []
        hit = self.v._events.get((sid, head))
        if hit is None:
            hit = []
            try:
                for i, (rid, c) in enumerate(self.v._receipts(sid)):
                    m = c.meta
                    hit.append({"i": i, "id": rid, "ev": m.get("ev", ""), "turn": int(m.get("turn", 0)),
                                "ts": float(m.get("ts", 0)), "items": [(r, int(b)) for r, b in zip(m.get("items", []), m.get("b", []))]})
            except (KeyError, ValueError, AttributeError, TypeError, OSError):
                hit = []
            if len(self.v._events) > 256:
                self.v._events.clear()
            self.v._events[(sid, head)] = hit
        return hit

    def walk_of(self, sid):
        s = self.S.get(sid)
        return self.v.walk(s["ref"]) if s and s["ref"] else None

    # -- the context axis ------------------------------------------------------------------------------------
    def axis(self):
        unit, S = self.unit, self.S
        if unit == "time":
            stamps = []
            for sid in self.order:
                s = S[sid]
                stamps += [t for t in (s["t0"], s["t1"]) if t is not None]
                stamps += [r["first_seen"] for r in self.cost_by.get(sid, ()) if r.get("first_seen") is not None]
                stamps += [r["ts"] for r in self.ace_by.get(sid[:ACE_PREFIX], ())]
                stamps += [r["ts"] for r in self.use_by.get(sid, ())]
                if s["ref"]:
                    stamps += [e["ts"] for e in self.events_of(sid)]
            if not stamps:
                return
            self.kind, starts = time_buckets(min(stamps), max(stamps))
            if len(starts) > COLS_MAX:
                starts, self.capped["context"] = starts[-COLS_MAX:], True
            self.starts = starts
            self.end = starts[-1] + 3600 if self.kind == "hour" else _midnight(starts[-1], 1)
            fmt = "%Y-%m-%d %H:00" if self.kind == "hour" else "%Y-%m-%d"
            for i, t in enumerate(starts):
                self.ctx.append({"i": i, "kind": "time", "t0": t, "t1": starts[i + 1] if i + 1 < len(starts) else self.end,
                                 "label": time.strftime(fmt, time.localtime(t))})
            return
        sid = self.focus
        if unit == "turn":
            walk, events = self.walk_of(sid), (self.events_of(sid) if S[sid]["ref"] else [])
            turns = walk.turns if walk else []
            count = max(len(turns), max([e["turn"] + 1 for e in events], default=0))
            for i in range(count):
                self.ctx.append({"i": i, "kind": "turn", "label": "turn %d" % (i + 1),
                                 "t0": turns[i][2] if i < len(turns) else None,
                                 "crumb": turns[i][1] if i < len(turns) else None})
            raw = [c["t0"] for c in self.ctx]
        elif unit == "event":
            events = self.events_of(sid) if S[sid]["ref"] else []
            for e in events:
                self.ctx.append({"i": e["i"], "kind": "event", "label": e["ev"], "ev": e["ev"], "turn": e["turn"],
                                 "t0": e["ts"], "rcpt": e["id"]})
            raw = [e["ts"] for e in events]
        else:  # request
            main = [r for r in self.use_by.get(sid, ()) if r["task"] == "main"]
            self.slots = request_slots(self.ace_by.get(sid[:ACE_PREFIX], []), main)
            if len(self.slots) > COLS_MAX:
                self.slots, self.capped["context"] = self.slots[-COLS_MAX:], True
            for i, sl in enumerate(self.slots):
                if sl["ace"] is not None:
                    self.slot_of[("ace", id(sl["ace"]))] = i
                if sl["usage"] is not None:
                    self.slot_of[("usage", id(sl["usage"]))] = i
                self.ctx.append({"i": i, "kind": "request", "label": "request %d" % (i + 1), "t0": sl["ts"],
                                 "act": sl["ace"]["act"] if sl["ace"] else None, "ace": sl["ace"] is not None,
                                 "usage": sl["usage"] is not None})
            raw = [sl["ts"] for sl in self.slots]
        known = [t for t in raw if t is not None]  # each column starts no earlier than the one before it
        self.anchors, top = [], known[0] if known else 0.0
        for t in raw:
            top = max(top, t) if t is not None else top
            self.anchors.append(top)

    def ctx_time(self, ts):
        if ts is None or not self.starts:
            return None
        i = bisect.bisect_right(self.starts, ts) - 1
        return None if i < 0 else min(i, len(self.starts) - 1)

    def ctx_log(self, row, kind):  # a line of usage.log or ace.log
        if self.unit == "time":
            return self.ctx_time(row["ts"])
        return self.slot_of.get((kind, id(row))) if self.unit == "request" else None

    def ctx_event(self, e):
        if self.unit == "time":
            return self.ctx_time(e["ts"])
        if self.unit == "turn":
            return min(e["turn"], len(self.ctx) - 1) if self.ctx else None
        return e["i"] if self.unit == "event" and e["i"] < len(self.ctx) else None

    def ctx_node(self, walk, i):
        if self.unit == "time":
            return self.ctx_time(walk.ts[i])
        return walk.turn[i] if self.unit == "turn" and walk.turn[i] < len(self.ctx) else None

    # -- the numbers of one session, by where they are recorded ----------------------------------------------
    def facts_db(self, sid):
        acc, s = {}, self.S[sid]
        c = self.ctx_time(s["t0"]) if self.unit == "time" and s["rec"] is not None else None
        for m, col in DB_COLUMN.items():
            if c is not None and m in self.want and s["rec"].get(col) is not None:
                _put(acc, (c, m, None, None), s["rec"][col], s["t0"], ["db", "sessions." + col, sid])
        return acc

    def facts_cost(self, sid):
        """The cost of a session at the grain it is recorded: one number for each (session, model, task) row of
        session_model_usage, at the time that row first saw a request. Nothing is priced here."""
        acc = {}
        if "cost_usd" not in self.want or self.unit != "time":
            return acc
        for row in self.cost_by.get(sid, ()):
            usd, col = recorded_cost(row)
            c = self.ctx_time(row.get("first_seen"))
            if usd is not None and c is not None:
                model, task = row.get("model") or "-", row.get("task") or "main"
                _put(acc, (c, "cost_usd", model, task), usd, row["first_seen"],
                     ["db", "session_model_usage", sid, model, row.get("task") or "", col, row.get("cost_status"),
                      row.get("cost_source")])
        rec = self.S[sid]["rec"]
        if not self.cost_by and rec is not None and self.db is not None:  # an older state.db: the session is the grain
            usd, col = (rec.get("actual_cost_usd"), "actual_cost_usd") if rec.get("actual_cost_usd") \
                else (rec.get("estimated_cost_usd"), "estimated_cost_usd")
            c = self.ctx_time(self.S[sid]["t0"])
            if usd is not None and c is not None:
                _put(acc, (c, "cost_usd", "-", "main"), usd, self.S[sid]["t0"], ["db", "sessions." + col, sid])
        return acc

    def facts_usage(self, sid):
        acc = {}
        wanted = [(m, f) for m, f in USAGE_FIELD.items() if m in self.want]
        for r in self.use_by.get(sid, ()) if wanted and self.unit in ("time", "request") else ():
            c = self.ctx_log(r, "usage")
            for m, f in wanted if c is not None else ():
                _put(acc, (c, m, r["model"] or "-", r["task"]), r[f], r["ts"], ("usage", sid))
        return acc

    def facts_ace(self, sid):
        acc, prefix = {}, sid[:ACE_PREFIX]
        if not any(MEASURE[m]["group"] == "ace" for m in self.want) or self.unit not in ("time", "request"):
            return acc
        for r in self.ace_by.get(prefix, ()):
            c = self.ctx_log(r, "ace")
            if c is None:
                continue
            vals = {"ace_raw": r["raw"], "ace_sent": r["sent"], "ace_held": max(0, r["raw"] - r["sent"]),
                    "ace_folds": 1 if r["act"] == "fold" else 0}
            if r["base"] is not None:
                vals["ace_base"] = r["base"]
            if r["lay"] is not None:
                vals["ace_lay"] = r["lay"]
            if r["base"] is not None and r["lay"] is not None:
                vals["ace_exact"] = max(0, r["sent"] - r["base"] - r["lay"])
            if r["target"] is not None:
                vals["ace_target"] = r["target"]
            for m, val in vals.items():
                if m in self.want:
                    _put(acc, (c, m, None, None), val, r["ts"], ("ace", prefix), self.agg[m])
        return acc

    def facts_tree(self, sid):
        """(own, incl) of the two measures that live in a session's crumb tree: own are the cells of the session's own row,
        incl the numbers of the whole tree (the session, its crumbs, and the crumbs of other sessions that its context
        drew in). The tree is not walked for ctx_bytes."""
        own, nums, s = {}, {}, self.S[sid]
        if not s["ref"]:
            return own, nums
        if "ctx_bytes" in self.want and self.unit in ("time", "turn", "event"):
            for e in self.events_of(sid):
                c = self.ctx_event(e)
                for ref, b in e["items"] if c is not None else ():
                    key = (c, "ctx_bytes", None, None)
                    nums[key] = nums.get(key, 0) + b
                    if ref == s["ref"]:
                        _put_rcpt(own, key, b, e["ts"], e["id"])
        if "size" in self.want and self.unit in ("time", "turn"):
            walk = self.walk_of(sid)
            for i in range(len(walk.refs)):
                c = self.ctx_node(walk, i)
                if c is not None:
                    key = (c, "size", None, None)
                    nums[key] = nums.get(key, 0) + walk.selfb[i]
                    if i == 0:
                        own[key] = _Cell(walk.selfb[0], walk.ts[0], ["store", walk.refs[0], "size"])
        return own, nums

    def numbers(self):
        """own[sid]: the cells of a session's own row; incl[sid]: the numbers of the session and everything that it
        includes: its crumbs, and the sessions under it in the lineage (each rolled up the same way)."""
        self.own, self.incl, tree = {}, {}, {}
        for sid in self.order:
            flat = {**self.facts_db(sid), **self.facts_cost(sid), **self.facts_usage(sid), **self.facts_ace(sid)}
            own_tree, nums = self.facts_tree(sid)
            self.own[sid] = {**flat, **own_tree}
            tree[sid] = _nums(flat)
            _roll(tree[sid], nums, self.agg)
        for sid in reversed(self.order):  # a session comes after the one that it hangs from, so this is children first
            total = dict(tree[sid])
            for child in self.kids[sid]:
                _roll(total, self.incl[child], self.agg)
            self.incl[sid] = total

    # -- the crumbs inside a session -------------------------------------------------------------------------
    def node_cells(self, sid, walk):
        """For each place in the crumb tree, its own cells: {(context, measure, model, task): _Cell}."""
        if sid not in self._cells:
            cells = [dict() for _ in walk.refs]
            if "size" in self.want and self.unit in ("time", "turn"):
                for i in range(len(walk.refs)):
                    c = self.ctx_node(walk, i)
                    if c is not None:
                        cells[i][(c, "size", None, None)] = _Cell(walk.selfb[i], walk.ts[i], ["store", walk.refs[i], "size"])
            if "ctx_bytes" in self.want and self.unit in ("time", "turn", "event"):
                for e in self.events_of(sid):
                    c = self.ctx_event(e)
                    for ref, b in e["items"] if c is not None else ():
                        i = walk.first.get(ref)
                        if i is not None:
                            _put_rcpt(cells[i], (c, "ctx_bytes", None, None), b, e["ts"], e["id"])
            self._cells[sid] = cells
        return self._cells[sid]

    def foreign_cells(self, sid, walk):
        """The crumbs that this session's context drew in from outside its own tree: {ref: {key: _Cell}}, in the order
        that the receipts first named them."""
        if sid not in self._foreign:
            out = {}
            if "ctx_bytes" in self.want and self.unit in ("time", "turn", "event"):
                for e in self.events_of(sid):
                    c = self.ctx_event(e)
                    for ref, b in e["items"] if c is not None else ():
                        if walk.first.get(ref) is None:
                            _put_rcpt(out.setdefault(ref, {}), (c, "ctx_bytes", None, None), b, e["ts"], e["id"])
            self._foreign[sid] = out
        return self._foreign[sid]

    def node_incl(self, cells, walk, i):
        out = {}
        for j in range(i, walk.end[i]):
            if cells[j]:
                _roll(out, _nums(cells[j]), self.agg)
        return out

    # -- rows and facts --------------------------------------------------------------------------------------
    def num(self, v, measure):
        return None if v is None else round(v, 9) if MEASURE[measure]["unit"] == "usd" else v

    def source(self, cell, measure):
        group = MEASURE[measure]["group"]
        if group in ("usage", "ace"):
            return list(cell.src) + [cell.n, cell.t0, cell.t1]
        return ["rcpt", list(cell.src[1]), cell.n] if group == "rcpt" else list(cell.src)

    def fact_rows(self, key, scope, own, incl):
        """[row, context, scope, measure, self, incl, source, model, task] for every number of one row. self is None when the
        row records none of its own (its members do), incl is the row and everything that it includes."""
        out = []
        for k in sorted(set(own) | set(incl), key=lambda k: (k[0], k[1], k[2] or "", k[3] or "")):
            cell = own.get(k)
            v = cell.v if cell else None
            out.append([key, k[0], scope, k[1], self.num(v, k[1]), self.num(incl.get(k, v), k[1]),
                        self.source(cell, k[1]) if cell else None, k[2], k[3]])
            if k[2]:
                self.models.add(k[2])
        return out

    def row_label(self, ref, kind, title, t0=None, sid=""):
        """(label, named) of a row. A chat and a span are CUED kinds: their label holds cue words, which cost a read of all the text
        under them, so the answer gives quick() and named False until names is set (or View.labels is asked)."""
        if kind in CUED and not self.names:
            return self.quick(kind, title, ref, t0, sid), False
        return self.label(ref), True

    def session_item(self, sid):
        s, v = self.S[sid], self.v
        c = self.crumb(s["ref"]) if s["ref"] else None
        more = bool(c is not None and (c.kids or v.index.summary_of(s["ref"]) or self.events_of(sid)))
        rec = s["rec"] or {}
        name, named = self.row_label(s["ref"], "sess", c.title if c else "", s["t0"], sid) if s["ref"] else (short_sid(sid), True)
        return {"key": _skey(sid), "parent": _skey(s["parent"]) if s["parent"] in self.kids and sid not in self.top else None,
                "kind": "sess", "label": name, "named": named, "short": short_sid(sid),
                "depth": self.depth.get(sid, 0), "has_kids": bool(self.kids.get(sid)) or more, "more": more,
                "scope": s["scope"], "sid": sid, "ref": s["ref"], "t0": s["t0"], "t1": s["t1"],
                "how": rec.get("created_source"), "db": s["rec"] is not None}

    def crumb_item(self, sid, walk, node, pos, parent, depth):
        ref = walk.refs[node]
        name, named = self.row_label(ref, walk.kind[node], walk.title[node], walk.ts[node], sid)
        return {"key": "%s>%d.%s" % (parent, pos, ref), "parent": parent, "kind": walk.kind[node], "label": name, "named": named,
                "depth": depth, "has_kids": bool(walk.kids[node]), "more": bool(walk.kids[node]),
                "scope": self.S[sid]["scope"], "sid": sid, "ref": ref, "t0": walk.ts[node], "t1": None}

    def parse(self, key):
        """(sid, path, group): path is [(position, ref)] down the crumb tree; group is None, or the rest of a key under
        the group of crumbs from other sessions."""
        parts = key.split(">")
        if not parts[0].startswith("s:") or unquote(parts[0][2:]) not in self.S:
            raise KeyError(key)
        sid, rest = unquote(parts[0][2:]), parts[1:]
        if rest and rest[0] == "~":
            return sid, [], rest[1:]
        path = []
        for seg in rest:
            pos, _, ref = seg.partition(".")
            if not pos.isdigit() or not ref:
                raise KeyError(key)
            path.append((int(pos), ref))
        return sid, path, None

    def expand(self, key, rows, facts):
        """The members of one row, one level: for a session, its crumbs and the group of crumbs that its context drew in
        from other sessions; for a crumb, its kids. (The sessions under a session are rows of the top level already.)"""
        sid, path, group = self.parse(key)
        walk = self.walk_of(sid)
        if walk is None:
            return
        scope, d0 = self.S[sid]["scope"], self.depth.get(sid, 0)
        if group is not None:
            if group:  # one crumb of the group: it has no members
                return
            for n, (ref, cs) in enumerate(self.foreign_cells(sid, walk).items()):
                c = self.v.crumb_or_none(ref) if "@" not in ref else None
                if c is not None and not self.v._hidden(c.meta.get("sess", "")):  # no-vis: no label
                    name, named = self.row_label(ref, c.kind, c.title)
                else:
                    name, named = ref, True
                item = {"key": "%s>%d.%s" % (key, n, ref), "parent": key, "kind": c.kind if c else "text",
                        "label": name, "named": named, "depth": d0 + 2, "has_kids": False,
                        "more": False, "scope": scope, "sid": sid, "ref": ref if c is not None else None,
                        "t0": None, "t1": None}
                rows.append(item)
                facts += self.fact_rows(item["key"], scope, cs, _nums(cs))
            return
        node = 0
        for pos, ref in path:
            kids = walk.kids[node]
            if pos >= len(kids) or walk.refs[kids[pos]] != ref:
                raise KeyError(key)
            node = kids[pos]
        cells = self.node_cells(sid, walk)
        for pos, j in enumerate(walk.kids[node]):
            item = self.crumb_item(sid, walk, j, pos, key, d0 + 1 + len(path))
            rows.append(item)
            facts += self.fact_rows(item["key"], scope, cells[j], self.node_incl(cells, walk, j))
        foreign = self.foreign_cells(sid, walk) if node == 0 and not path else {}
        if foreign:
            gkey = key + ">~"
            rows.append({"key": gkey, "parent": key, "kind": "group", "label": "from other sessions", "named": True, "depth": d0 + 1,
                         "has_kids": True, "more": True, "scope": scope, "sid": sid, "ref": None, "t0": None, "t1": None})
            total = {}
            for cs in foreign.values():
                _roll(total, _nums(cs), self.agg)
            facts += self.fact_rows(gkey, scope, {}, total)

    # -- the line graph --------------------------------------------------------------------------------------
    def x_of(self, ts):
        """Where a time falls on the context axis, in columns (column i spans i to i+1)."""
        if self.unit == "time":
            return place(ts, self.starts, self.end) if self.starts else 0.0
        if self.unit == "request":
            return place(ts, [sl["ts"] for sl in self.slots], max([sl["ts"] for sl in self.slots] + [ts]) + 1) \
                if self.slots else 0.0
        return place(ts, self.anchors, max(self.anchors + [ts]) + 1) if self.anchors else 0.0

    def series(self, sid, role, depth):
        s, v = self.S[sid], self.v
        use, prefix = self.use_by.get(sid, []), sid[:ACE_PREFIX]
        main, aux = [r for r in use if r["task"] == "main"], [r for r in use if r["task"] != "main"]
        ace = self.ace_by.get(prefix, [])
        on_slots = self.unit == "request"

        def point(r, kind, middle):
            slot = self.slot_of.get((kind, id(r))) if middle else None
            x = round(slot + 0.5 if slot is not None else self.x_of(r["ts"]), 4)
            if kind == "ace":
                return {"x": x, "ts": r["ts"], "act": r["act"], "sent": r["sent"], "raw": r["raw"], "base": r["base"],
                        "lay": r["lay"], "target": r["target"], "folds": r["folds"]}
            self.models.add(r["model"] or "-")
            return {"x": x, "ts": r["ts"], "task": r["task"], "model": r["model"], "prompt": r["prompt"], "in": r["in"],
                    "cr": r["cr"], "cw": r["cw"], "out": r["out"], "rsn": r["rsn"], "line": r["line"], "file": r["file"],
                    "no": r["no"]}

        if on_slots:  # a request that is not on the axis (cut by the cap) is not drawn
            main = [r for r in main if ("usage", id(r)) in self.slot_of]
            ace = [r for r in ace if ("ace", id(r)) in self.slot_of]
        if len(main) > POINTS_MAX or len(ace) > POINTS_MAX:
            self.capped["points"] = True
        name = (self.label(s["ref"]) if self.names else "sess " + short_sid(sid)) if s["ref"] else short_sid(sid)  # see quick()
        return {"key": _skey(sid), "sid": sid, "label": name,
                "short": short_sid(sid), "role": role, "depth": depth, "n": len(main), "n_aux": len(aux),
                "points": [point(r, "usage", on_slots) for r in main[-POINTS_MAX:]],
                "aux": [point(r, "usage", False) for r in aux[-POINTS_MAX // 4:]],
                "ace": [point(r, "ace", on_slots) for r in ace[-POINTS_MAX:]]}

    def build_lines(self):
        """The context line graph of the focus session: its requests, each with the tokens that the provider counted (usage.log)
        and, where ace.log has the session, what ACE sent. With incl, one more line for each session under it."""
        sid, members, queue, seen = self.focus, [], [(c, 1) for c in self.children_all.get(self.focus, ())], {self.focus}
        while queue and self.lines_of_family and self.unit == "time":
            child, d = queue.pop(0)
            if child not in seen:
                seen.add(child)
                members.append((child, d))
                queue += [(c, d + 1) for c in self.children_all.get(child, ())]
        members = [(c, d) for c, d in members if self.use_by.get(c) or self.ace_by.get(c[:ACE_PREFIX])]
        total = len(members)
        members.sort(key=lambda m: (-len(self.use_by.get(m[0], ())), self.S[m[0]]["t0"] or 0))
        series = [self.series(sid, "focus", 0)] + [self.series(c, "member", d) for c, d in members[:LINES_MAX - 1]]
        return {"unit": self.unit, "series": series, "members": total, "shown": len(series) - 1}

    # -- the answer ------------------------------------------------------------------------------------------
    def answer(self):
        rows, facts, totals = [], [], None
        if self.roots:
            for key in self.roots:
                self.expand(key, rows, facts)
        else:
            self.numbers()
            for sid in self.order[:self.limit]:
                item = self.session_item(sid)
                rows.append(item)
                facts += self.fact_rows(item["key"], item["scope"], self.own[sid], self.incl[sid])
            self.capped.update(total_rows=len(self.order), rows=len(self.order) > self.limit)
            total = {}
            for r in self.top:  # the lineage roots hold everything below them: this is the grand total, in either mode
                _roll(total, self.incl[r], self.agg)
            totals = [[k[1], k[0], self.num(v, k[1]), k[2], k[3]]
                      for k, v in sorted(total.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2] or "", kv[0][3] or ""))]
        lines = self.build_lines() if self.want_lines and self.focus else None
        has = {"db": self.db is not None, "cost": self.db is not None, "usage": bool(self.use_by), "ace": bool(self.ace_by),
               "rcpt": any(self.events_of(s["sid"]) for s in self.S.values() if s["ref"]),
               "store": any(s["ref"] for s in self.S.values())}
        return {"unit": self.unit, "bucket": self.kind if self.unit == "time" else "", "focus": self.focus,
                "root": self.roots, "has_db": self.db is not None, "profile": self.profile, "limit": self.limit,
                "capped": self.capped,
                "dims": {"scope": self.scopes, "family": rows, "context": self.ctx,
                         "measure": [dict(m, has=has[m["group"]]) for m in MEASURES], "model": sorted(self.models)},
                "facts": facts, "totals": totals, "lines": lines}


class View:
    """Read-only access to one breadcrumbs folder (the provider's root)."""

    def __init__(self, root, clock=time.time, hide=None):
        """Open a store to read. A View creates and changes no file: a folder without a store raises
        FileNotFoundError. close() releases the files. hide: for the views that draw (the explorer, term, spacegraph,
        the desktop page), novis.Hide or any hide(sid, prefix=False) -> bool: a session that it hides is left out of
        every answer and refused by id, and so are its crumbs. None (bc, vault, core, hive): every session."""
        if not os.path.isfile(os.path.join(root, "index.db")):
            raise FileNotFoundError(os.path.join(root, "index.db"))
        self.root, self.clock, self.hide, self._closed = root, clock, hide, False
        self.store = Store(os.path.join(root, "store"), readonly=True)
        try:
            self.index = Index(os.path.join(root, "index.db"), readonly=True)
            self.refs = Refs(os.path.join(root, "refs"), readonly=True)
        except BaseException:
            self.store.close()
            raise
        self.zoom = Zoom(self.store, summaries=self.index.summary_of, cues=self.index.signature,
                         words=self.index.tiers)
        self.lock = threading.Lock()
        self.db_connect = None  # sqlite3.connect for state.db (a test can pass one that watches the reads)
        self._walks, self._events = OrderedDict(), {}  # measures(): crumb trees by session crumb id, receipts by head
        self._nul, self._nul_held = {}, 0  # measures(): block of the store -> where its NUL bytes are, and how many offsets (see _Sizer)

    def _info(self, ref):
        blob_id = ref.partition(":")[0]
        blob = self.store.get(blob_id)
        info = {"id": ref, "kind": "text", "title": "", "cues": [], "session": "", "ts": 0,
                "tokens": self.zoom.full(ref)}
        if is_crumb(blob) and ":" not in ref:
            c = Crumb.decode(blob)
            info.update(kind=c.kind, title=c.title, session=c.meta.get("sess", ""), ts=c.meta.get("ts", 0),
                        cues=self.zoom.cue_words(blob_id).split() if c.kind in CUED else [])
        return info

    def _weigh(self, nodes):
        """NodeVis.pri in (0, 1] from the size (log scale); heat in [0, 1] from the last use."""
        top = max([n["tokens"] for n in nodes] + [1])
        used, now = self.index.heat([n["id"] for n in nodes]), self.clock()
        for n in nodes:
            n["pri"] = round((1 + math.log1p(n["tokens"])) / (1 + math.log1p(top)), 4)
            last = used.get(n["id"], (None,))[0]
            n["heat"] = round(0.5 ** (max(0.0, now - last) / HEAT_HALF), 4) if last else 0.0

    def graph(self, root=None, limit=300):
        """Nodes and edges. No root: the sessions that moved last, joined where they share cue words.
        A root: the crumb and its parts, one level (its summary first, if it has one)."""
        with self.lock:
            if root is None:
                names = [n for n in self.refs.names() if n.startswith("sess-")]
                moved = {n: os.stat(os.path.join(self.refs.path, n)).st_mtime_ns for n in names}
                names.sort(key=lambda n: (moved[n], n), reverse=True)
                refs = [self.refs.get(n) for n in names[:limit]]
                nodes = [n for n in (self._info(r) for r in refs if r) if not self._hidden(n["session"])]
                edges = cue_edges(nodes)
            else:
                if self._hidden_ref(root):
                    raise KeyError(root)
                blob = self.store.get(root.partition(":")[0])
                parts = [] if ":" in root or not is_crumb(blob) else \
                    self.zoom.parts(Crumb.decode(blob), root)[:limit]
                nodes = [self._info(root)] + [self._info(p) for p in parts]
                edges = [{"source": root, "target": p, "kind": "part", "w": 1.0} for p in parts]
                for e, n in zip(edges, nodes[1:]):
                    if n["kind"] == "sum":
                        e["kind"] = "sum"
            self._weigh(nodes)
            return {"root": root, "nodes": nodes, "edges": edges}

    def crumb(self, ref, level):
        with self.lock:
            if self._hidden_ref(ref):
                raise KeyError(ref)
            return {"id": ref, "level": level, "tokens": self.zoom.full(ref), "text": self.zoom.render(ref, level)}

    def find(self, q, k=8):
        """Search hits grouped by session: [{session, hits: [{id, ref, score}]}], best first."""
        from .hermes import _ref_name
        nb = dials.value(dials.config(self.root), None, "neighbor")
        with self.lock:
            groups, n = {}, 0
            for cid, ref, score in self.index.search(q, 3 * k, nb=nb):
                blob = self.store.get(cid)
                sid = Crumb.decode(blob).meta.get("sess", "") if is_crumb(blob) else ""
                if self._hidden(sid):
                    continue
                sess = self.refs.get(_ref_name("sess-", sid)) if sid else None
                groups.setdefault(sess or cid, {"session": sess, "hits": []})["hits"].append(
                    {"id": cid, "ref": ref, "score": round(score, 3)})
                n += 1
                if n >= k:
                    break
            return list(groups.values())

    def path(self, ref, within=None, depth=4, budget=5000):
        """The crumbs from a start down to ref: [start, ..., ref], or [] when ref is not found.
        The start is `within`, else the session of ref (a text does not know its session)."""
        from .hermes import _ref_name
        with self.lock:
            if self._hidden_ref(ref):
                return []
            blob = self.store.get(ref.partition(":")[0])
            start = within
            if start is None and is_crumb(blob) and ":" not in ref:
                sid = Crumb.decode(blob).meta.get("sess", "")
                start = self.refs.get(_ref_name("sess-", sid)) if sid else None
            if not start:
                return []
            up: dict = {start: None}
            level, seen = [start], 0
            for _ in range(depth + 1):
                if ref in up:
                    out = [ref]
                    while up[out[-1]] is not None:
                        out.append(up[out[-1]])
                    return out[::-1]
                below = []
                for r in level:
                    b = self.store.get(r.partition(":")[0])
                    if ":" in r or not is_crumb(b):
                        continue
                    for p in self.zoom.parts(Crumb.decode(b), r):
                        if p not in up and seen < budget:
                            up[p], seen = r, seen + 1
                            below.append(p)
                level = below
            return []

    def _crumb(self, ref):
        blob = self.store.get(ref.partition(":")[0])
        return Crumb.decode(blob) if ":" not in ref and is_crumb(blob) else None

    def _row(self, ref):
        c = self._crumb(ref)
        sid = self._sid(ref, c)
        return {"id": ref, "kind": c.kind if c else "text", "label": ref if self._hidden(sid) else label(self.zoom, ref),
                "session": sid, "bars": []}

    def _sid(self, ref, c):
        """The session id of a crumb, or of the first indexed crumb that holds a text ("" when not known)."""
        if c is None:
            holder = self.index.holder(ref.partition(":")[0])
            c = self._crumb(holder) if holder else None
        return c.meta.get("sess", "") if c else ""

    def _hidden(self, sid, prefix=False):
        """True when self.hide hides session sid ("": a crumb of no session, hidden when the whole profile is)."""
        return self.hide is not None and bool(self.hide(sid or "", prefix))

    def _hidden_ref(self, ref):
        """True when ref (a crumb id, or a text "doc:a-b") is of a session that self.hide hides."""
        if self.hide is None:
            return False
        try:
            return self._hidden(self._sid(ref, self._crumb(ref)))
        except (KeyError, ValueError, OSError):
            return self._hidden("")

    def session(self, sid):
        """One session as nodes and edges (the graph that the explorer and the Desktop page draw).
        nodes: id, kind, title, tokens, meta, ranges, cues (sess, span), summary, use {accessed, uses}.
        edges: kid (a crumb -> each kid, in order), heritage (a derived crumb -> its source),
        nbr (a turn -> the turn it follows), call (a tool crumb -> the step crumb that called it)."""
        from .hermes import _ref_name
        with self.lock:
            top = self.refs.get(_ref_name("sess-", sid))
            if not top or self._hidden(sid):
                raise KeyError(sid)
            ids, stack, seen = [], [top], set()
            while stack:
                cid = stack.pop()
                if cid not in seen:
                    seen.add(cid)
                    ids.append(cid)
                    stack.extend(reversed(self._crumb(cid).kids))
            sums = {cid: self.index.summary_of(cid) for cid in ids}
            ids += [x for x in dict.fromkeys(sums.values()) if x and x not in seen]
            heat = self.index.heat(ids)
            nodes, edges, known = [], [], set(ids)
            for cid in ids:
                c = self._crumb(cid)
                node = {"id": cid, "kind": c.kind, "title": c.title, "tokens": self.zoom.full(cid), "meta": c.meta,
                        "ranges": c.ranges}
                if c.kind in CUED:
                    node["cues"] = self.zoom.cue_words(cid).split()
                if sums.get(cid):
                    node["summary"] = sums[cid]
                if cid in heat:
                    node["use"] = {"accessed": heat[cid][0], "uses": heat[cid][1]}
                nodes.append(node)
                edges += [{"kind": "kid", "source": cid, "target": k, "w": 1.0} for k in c.kids]
                edges += [{"kind": "heritage", "source": cid, "target": h, "w": 1.0} for h in c.heritage]
                if c.meta.get("step") in known:
                    edges.append({"kind": "call", "source": cid, "target": c.meta["step"], "w": 1.0})
            order = {cid: i for i, cid in enumerate(ids)}
            marks = ",".join("?" * len(ids))
            for a, b in self.index.db.execute("SELECT a, b FROM nbr WHERE a IN (%s) AND b IN (%s)" % (marks, marks),
                                              ids + ids):
                if order[a] < order[b]:  # stored both ways: keep prev -> cur once, as cur follows prev
                    edges.append({"kind": "nbr", "source": b, "target": a, "w": 1.0})
            return {"session": sid, "root": top, "nodes": nodes, "edges": edges}

    def _band(self):
        """The controller's band from horizon.json, for receipts that do not record it (or None)."""
        try:
            with open(os.path.join(self.root, "horizon.json"), encoding="utf-8") as f:
                return json.load(f).get("band")
        except (OSError, ValueError):
            return None

    def _receipts(self, sid):
        """The context receipts of a session, oldest first: [(id, crumb)]."""
        from .hermes import _ref_name
        out, rid = [], self.refs.get(_ref_name("rcpt-", sid))
        while rid:
            c = self._crumb(rid)
            out.append((rid, c))
            rid = c.meta.get("prev") or None
        return out[::-1]

    def context(self, sid):
        """The Gantt of a session's context. events: one per receipt (recall, open, find, compress, worker) with
        its turn, time, bytes, budget, ovf, rate, band and q (the query of a recall or a find, the brief of a
        worker, else ""). A worker event is a delegation: it has no bar and no part in the turn budget.
        With the turn budget: turn_budget (bytes; None when off), used (the bytes of recall, find and open
        in this turn so far, with the event), cap (the turn budget cut it) and want (its bytes before the
        cut; = bytes when not cut).
        turns: [{turn, crumb, ts}] for each user turn. rows: [{id, kind, label, session, bars}]; session
        is the sid of the crumb (of the crumb that holds a text; "" when not known). A bar is one copy of a
        crumb in the context:
        {from, to, t0, t1, level, bytes, ev}. from/to are event indexes, t0/t1 turns; to and t1 are None
        while the copy is live. A bar ends at the next compress event. The session's own crumbs are rows
        too (ev "own", level 1): from their turn to the first compress after they were stored."""
        from .hermes import _ref_name
        with self.lock:
            if self._hidden(sid):
                raise KeyError(sid)
            chain = self._receipts(sid)
            sess = self.refs.get(_ref_name("sess-", sid))
            if not chain and not sess:
                raise KeyError(sid)
            band = self._band()
            events, spent = [], {}
            for rid, c in chain:
                m = c.meta
                if m["ev"] in ADDING:  # the turn budget counts recall, find and open
                    spent[m["turn"]] = spent.get(m["turn"], 0) + m["bytes"]
                events.append({"id": rid, "ev": m["ev"], "turn": m["turn"], "ts": m["ts"], "bytes": m["bytes"],
                               "budget": m.get("budget", 0), "ovf": m.get("ovf", False), "rate": m.get("rate", 0.0),
                               "band": m.get("band") or band, "q": m.get("q", ""), "cap": bool(m.get("cap", False)),
                               "used": m.get("used", spent.get(m["turn"], 0)), "turn_budget": m.get("tb") or None,
                               "want": m.get("want", m["bytes"])})
            stops = [i for i, e in enumerate(events) if e["ev"] == "compress"]
            rows, turns, t = {}, [], -1
            for cid in leaves(self.store, sess) if sess else []:
                c = self._crumb(cid)
                if c.meta.get("role") == "user":
                    t += 1
                    turns.append({"turn": t, "crumb": cid, "ts": c.meta.get("ts", 0)})
                j = next((k for k in stops if events[k]["turn"] > max(t, 0)), None)  # turns, not seconds
                rows.setdefault(cid, self._row(cid))["bars"].append(
                    {"from": None, "to": j, "t0": max(t, 0), "t1": None if j is None else events[j]["turn"],
                     "level": 1.0, "bytes": self.zoom.size(cid), "ev": "own"})
            for i, (_, c) in enumerate(chain):
                m = c.meta
                if m["ev"] == "worker":  # a delegation sends names to a child: no copy in this context
                    continue
                j = next((k for k in stops if k > i), None)
                for ref, lv, b in zip(m["items"], m["lv"], m["b"]):
                    rows.setdefault(ref, self._row(ref))["bars"].append(
                        {"from": i, "to": j, "t0": m["turn"], "t1": None if j is None else events[j]["turn"],
                         "level": lv, "bytes": b, "ev": m["ev"]})
            return {"session": sid, "events": events, "turns": turns, "rows": list(rows.values())}

    def venn(self, sid, at=None):
        """The sets of a session's context at one event (default: the last), since the compress before
        it: recalled, found and opened crumbs. Each set and each region has its ids (exact) and its bytes
        in the context (every copy counts). The regions split the union of the sets; the "by" of a region
        gives the bytes of each set's copies, so a drawing can scale each overlap."""
        from .hermes import _ref_name
        with self.lock:
            if self._hidden(sid):
                raise KeyError(sid)
            chain = self._receipts(sid)
            if not chain:
                if not self.refs.get(_ref_name("sess-", sid)):
                    raise KeyError(sid)
                return {"session": sid, "at": None, "regions": [],
                        "sets": {n: {"ids": [], "bytes": 0} for n in ("recalled", "found", "opened")}}
            at = len(chain) - 1 if at is None else int(at)
            if not 0 <= at < len(chain):
                raise ValueError("at: an event index from 0 to %d" % (len(chain) - 1))
            start = max([i for i in range(at + 1) if chain[i][1].meta["ev"] == "compress"], default=-1) + 1
            names = {"recall": "recalled", "find": "found", "open": "opened"}
            sets = {n: {"ids": [], "bytes": 0} for n in ("recalled", "found", "opened")}
            per = {}  # id -> {set name: bytes}
            for _, c in chain[start:at + 1]:
                name = names.get(c.meta["ev"])
                if not name:
                    continue
                for ref, b in zip(c.meta["items"], c.meta["b"]):
                    if ref not in sets[name]["ids"]:
                        sets[name]["ids"].append(ref)
                    sets[name]["bytes"] += b
                    per.setdefault(ref, {}).setdefault(name, 0)
                    per[ref][name] += b
            regions = {}
            for ref, by in per.items():
                key = tuple(sorted(by))
                reg = regions.setdefault(key, {"in": list(key), "ids": [], "bytes": 0, "by": dict.fromkeys(key, 0)})
                reg["ids"].append(ref)
                reg["bytes"] += sum(by.values())
                for name, b in by.items():
                    reg["by"][name] += b
            return {"session": sid, "at": at, "sets": sets, "regions": [regions[k] for k in sorted(regions)]}

    def ace(self, sid=None, per=0):
        """The size of the context over time, from <root>/ace.log (ACE's own record of every request).
        current: the session of the newest line; session: the one shown (sid, else the current one; a log line
        holds a prefix of the session id, so sid matches by prefix; "" when the log has none of it).
        points: one per request {i, ts, act, raw, sent, seen, base, crumbs, lay, folds, target, drop}: raw is the
        request before ACE, sent the request after it, seen the model's own count of what was sent. A fold shows
        as a point whose act is fold (or ratchet, when the target rose in place of a fold) and a drop of sent.
        candles: open, high, low, close of sent over runs of `per` requests. mix: the last request split into
        base, layer, exact and held. all: the totals of every session in the log (ace_totals). Reads only."""
        with self.lock:
            by = {}
            for r in ace_requests(self.root):
                by.setdefault(r["sess"], []).append(r)
            by.pop("-", None)  # a request that no session owns
            for sess in [s for s in by if self._hidden(s, True)]:  # a line holds the first characters of the id
                del by[sess]
            series = {sess: ace_series(rows) for sess, rows in by.items()}
            newest = max(series, key=lambda k: series[k][-1]["ts"], default="")
            want = str(sid or "")
            hit = want if want in series else max((k for k in series if want.startswith(k) or k.startswith(want)),
                                                   key=len, default="") if want else newest
            pts = series.get(hit, [])
            return {"current": newest, "session": hit, "target": next((p["target"] for p in reversed(pts) if p["target"]), None),
                    "points": pts, "candles": ace_candles(pts, per), "mix": ace_mix(pts), "all": ace_totals(series)}

    def crumb_or_none(self, ref):
        """The crumb with this id, or None when the store has no crumb of that id."""
        try:
            return self._crumb(ref)
        except (KeyError, ValueError, OSError):
            return None

    def walk(self, ref):
        """The crumb tree under a session crumb, flat. Kept while the trees held are under WALK_NODES places in all (a
        crumb id never changes, so a tree never goes stale); the one used longest ago goes first."""
        w = self._walks.get(ref)
        if w is None:
            w = self._walks[ref] = _Walk(self, ref)
            total = sum(len(x.refs) for x in self._walks.values())
            while total > WALK_NODES and len(self._walks) > 1:
                total -= len(self._walks.popitem(last=False)[1].refs)
        else:
            self._walks.move_to_end(ref)
        return w

    def measures(self, root=None, unit="time", sid="", m=None, codebase="", branch="", incl=False, lines=False,
                 limit=ROWS_MAX, names=False):
        """Numbers with four independent keys, as one fact table that the page pivots. The view never puts two keys in one
        visual channel: scope is a control, context is the column axis, family is the row axis, inclusion is a toggle.

        SCOPE   where the work ran: profile (the store) > codebase > branch. A session's codebase is state.db
                sessions.git_repo_root, else the codebase that holds sessions.cwd (island.root_of), else "none"; its
                branch is sessions.git_branch. A store with no state.db has one scope, the profile. codebase= and
                branch= keep only the sessions of that scope (their roll-ups too). dims.scope lists all scopes.
        CONTEXT the columns, dims.context: unit "time" has hour buckets (the data spans 72 hours or fewer) or day buckets
                over the whole store; "turn", "request" and "event" need sid, the session in focus, and are its user
                turns, its requests (ace.log lines and the usage.log line of task main that follows each) or its context
                events (receipts: recall, find, open, compress). A number sits where its subject starts; a number that
                its source does not place on a unit has no cell there ("no data", never a guess): the measure's
                dims.measure[].units lists the units that place it.
        FAMILY  the rows, dims.family [{key, parent, kind, label, named, depth, has_kids, more, scope, sid, ref, t0, t1}]. The
                first call (no root) gives the session lineage (state.db sessions.parent_session_id: a chat, its
                compression continuations, its branches, the helpers it delegated), newest root first, at most `limit`
                (300) rows; capped says so. With a unit of turn, request or event it gives the focus session alone.
                root="key|key" gives the members of those rows, one level: for a session its crumbs (session > span >
                turn > step > tool) and a group "from other sessions" (crumbs that its context drew in from outside its
                tree); for a crumb its kids. A row with `more` has members to fetch. The crumb tree of a chat is read
                when a row of it opens (root=), never for the first call, except for a measure that walks (see m).
                LABELS  the label of a chat or a span holds three cue words, which cost a read of all the text under it (a
                minute, for a big store). So the answer gives a quick label (a chat: its start time; a span: its title) and
                named False, and View.labels(refs) gives the full label of those rows later; names=True gives them at once.
        INCLUSION  each fact has self (the row's own number) and incl (the row plus every member that it includes, to
                the bottom: its descendants in the lineage and in the crumb tree). incl = self + the incl of each member,
                exactly (max in place of + for a measure of agg max), and the grand total, totals, is the incl of the
                roots, which is also the sum of all self numbers. A heritage edge (a sum's sources) is a link, not a count.

        facts: [[row key, context, scope key, measure, self|None, incl, source, model|None, task|None]]. Cost and the
        usage.log measures are also keyed by model and task, which are never part of the row's label. source says where
        the number was read, so a click can show it: ["db", "sessions.<column>", sid]; ["db", "session_model_usage", sid,
        model, task, "<cost column>", cost_status, cost_source]; ["usage", sid, n, first, last] and ["ace", log prefix, n,
        first, last] (n lines, their first and last time); ["rcpt", [receipt crumb ids], n]; ["store", crumb id, "size"].

        m: the measures to compute, "a,b" (default: every measure that does not walk; "all": every measure; "none": no
        facts). A measure walks when it is read from the crumb tree of every chat (dims.measure[].walks): that is the size.
        It reads the crumbs and the spines of every chat (a few seconds for a store of 23 MB), so the default leaves it out;
        m="size" or m="all" asks for it. lines: also the context line graph of the focus session (usage.log prompt tokens by
        request, ACE's sent size where ace.log has the session, a point carries its usage.log line); incl: one more line for
        each session under it. Numbers and ids only: nothing is read from state.db but the columns of SESSION_COLS and
        MODEL_USAGE_COLS, never the messages. Nothing is written."""
        if unit not in UNITS:
            raise ValueError("unit: one of %s" % ", ".join(UNITS))
        if unit != "time" and not sid:
            raise ValueError("sid: the session whose turns, requests or events are the columns")
        if m is None:
            want = {k for k, d in MEASURE.items() if not d["walks"]}
        else:
            asked = {k for k in (m.split(",") if isinstance(m, str) else m) if k and k != "none"}
            want = (asked - {"all"}) | (set(MEASURE) if "all" in asked else set())
            if want - set(MEASURE):
                raise ValueError("m: unknown measure %s" % ", ".join(sorted(want - set(MEASURE))))
        roots = [k for k in root.split("|") if k] if isinstance(root, str) else list(root or [])
        return _Measures(self, roots, unit, sid or "", want, codebase or "", branch or "", bool(incl), bool(lines),
                         max(1, min(ROWS_MAX, int(limit))), bool(names)).run()

    def labels(self, refs):
        """The full label (the kind, then three cue words: see label()) of each crumb in refs, {ref: label}, at most LABELS_MAX
        of them. The first answer of measures() leaves these out because a label with cue words reads all the text under its
        crumb; the page asks for them here, a few at a time. The lock is taken for one label at a time, so another request
        waits for one label and not for the batch. A ref that this store cannot show is its own label."""
        out = {}
        for ref in list(refs)[:LABELS_MAX]:
            with self.lock:
                try:
                    out[ref] = ref if self._hidden_ref(ref) else label(self.zoom, ref)
                except (KeyError, ValueError, IndexError, OSError):
                    out[ref] = ref
        return out

    def _edge_ts(self, ref, last):
        """The time of the first (or last) leaf of a crumb: the way down follows one kid, so it reads a few crumbs, not the tree."""
        c = self.crumb_or_none(ref)
        while c is not None and c.kids:
            c = self.crumb_or_none(c.kids[-1 if last else 0])
        return c.meta.get("ts") if c is not None else None

    def calendar(self, limit=CAL_MAX):
        """When each session of the map ran, for the calendar layout (a day planner: one band for each day, one block for each
        session). The sessions are the ones that graph() lists (the session crumbs that moved last), at most `limit`. Answer:
        {sessions: [{id, sid, title, t0, t1, parent, how}], has_db, total, capped}, newest start first. It costs a read of a
        few columns and a few crumbs; graph() reads the cue words of every session, which is a read of all the text (39 s for
        126 chats), so the calendar page does not wait for it.
          id      the session crumb (a node of the map), sid its session id, title the title of the crumb (its first turn).
          t0, t1  seconds since the epoch. t0 is sessions.started_at; t1 is the later of ended_at and last_activity_at, so a
                  chat with no end runs to its last activity. A chat that state.db does not have, or that lacks a time, gets
                  the time of its first (or last) leaf in the store (_edge_ts: a few crumbs, never the whole tree). A chat
                  with no time at all has t0 and t1 None; a t1 before t0 is cut to t0.
          parent  the session id that this chat continues or was delegated by (sessions.parent_session_id), or None. It can
                  name a chat that the map does not show.
          how     sessions.created_source (cli, subagent, ...), or None.
        has_db says whether the profile has a state.db with sessions. Reads CAL_COLS of state.db and a few crumbs. Writes nothing."""
        db = read_times(os.path.join(os.path.dirname(os.path.abspath(self.root)), "state.db"), self.db_connect)
        limit = max(1, min(CAL_MAX, int(limit)))
        with self.lock:
            names = [n for n in self.refs.names() if n.startswith("sess-")]
            moved = {n: os.stat(os.path.join(self.refs.path, n)).st_mtime_ns for n in names}
            names.sort(key=lambda n: (moved[n], n), reverse=True)
            out = []
            for name in names[:limit]:
                ref = self.refs.get(name)
                c = self.crumb_or_none(ref) if ref else None
                sid = c.meta.get("sess", "") if c is not None else ""
                if not sid or self._hidden(sid):
                    continue
                rec = (db or {}).get(sid) or {}
                t0 = rec.get("started_at") or None
                ends = [x for x in (rec.get("ended_at"), rec.get("last_activity_at")) if x]
                t1 = max(ends) if ends else None
                if t0 is None:
                    t0 = self._edge_ts(ref, False) or None
                if t1 is None:
                    t1 = self._edge_ts(ref, True) or None
                if t0 is None:
                    t0 = t1
                if t1 is None or t1 < t0:
                    t1 = t0
                parent = rec.get("parent_session_id") or None
                out.append({"id": ref, "sid": sid, "title": c.title, "t0": None if t0 is None else float(t0),
                            "t1": None if t1 is None else float(t1), "parent": parent if parent != sid else None,
                            "how": rec.get("created_source") or None})
        out.sort(key=lambda s: (-(s["t0"] or 0), s["sid"]))
        return {"sessions": out, "has_db": db is not None, "total": len(names), "capped": len(names) > limit}

    def close(self):
        """Release the store and index files (a second call does nothing)."""
        with self.lock:
            if not self._closed:
                self._closed = True
                self.store.close()
                self.index.close()

    def trail(self):
        """The zoom stack of the chat in progress (trail.json). With hide: none for a chat that it hides, and no crumb of
        a session that it hides."""
        try:
            with open(os.path.join(self.root, "trail.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return Trail().to_dict()
        if self.hide is None or not isinstance(d, dict):
            return d
        with self.lock:
            if self._hidden(d.get("session", "")):
                return Trail().to_dict()
            stack = [e for e in d.get("stack", []) if not self._hidden_ref(str(e.get("id", "")))]
        return d if len(stack) == len(d.get("stack", [])) else Trail(stack, d.get("dropped", 0), d.get("session", "")).to_dict()


def cue_edges(nodes, per_node=4):
    """Edges between nodes that share cue words; weight = Jaccard; at most per_node edges each."""
    sets = [{w.lower() for w in n["cues"]} for n in nodes]
    pairs = []
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            shared = sets[i] & sets[j]
            if shared:
                pairs.append((len(shared) / len(sets[i] | sets[j]), i, j, sorted(shared)))
    pairs.sort(key=lambda p: -p[0])
    count, out = [0] * len(nodes), []
    for w, i, j, shared in pairs:
        if count[i] < per_node and count[j] < per_node:
            count[i] += 1
            count[j] += 1
            out.append({"source": nodes[i]["id"], "target": nodes[j]["id"], "kind": "cue", "w": round(w, 4),
                        "shared": shared})
    return out


def open_view(root):
    return View(root)


API = {
    "/api/graph": lambda v, q: v.graph(root=q.get("root") or None),
    "/api/crumb": lambda v, q: v.crumb(q["id"], min(1.0, max(0.0, float(q.get("level", "0.25"))))),
    "/api/find": lambda v, q: v.find(q.get("q", ""), max(1, min(50, int(q.get("n", "8"))))),  # k is the token
    "/api/trail": lambda v, q: v.trail(),
    "/api/path": lambda v, q: v.path(q["id"], within=q.get("within") or None),
    "/api/session": lambda v, q: v.session(q["sid"]),
    "/api/context": lambda v, q: v.context(q["sid"]),
    "/api/venn": lambda v, q: v.venn(q["sid"], q.get("at") or None),
    "/api/ace": lambda v, q: v.ace(q.get("sid") or None, max(0, min(500, int(q.get("per", "0"))))),
}
# Routes of the explorer page only. API is the table that the desktop dashboard mirrors (tests/test_dashboard.py pins the two
# to the same routes), so a route that the dashboard does not have lives here. To give the dashboard the numbers of the
# Measures panel too: move the entry into API, add the route to dashboard/plugin_api.py and "measures" to that test.
EXPLORER_API = {
    "/api/measures": lambda v, q: v.measures(q.get("root") or None, q.get("unit", "time"), q.get("sid", ""), q.get("m"),
                                             q.get("codebase", ""), q.get("branch", ""), q.get("incl") == "1",
                                             q.get("lines") == "1", int(q.get("limit", ROWS_MAX)), q.get("names") == "1"),
    "/api/measures/labels": lambda v, q: v.labels([r for r in q.get("refs", "").split("|") if r]),  # {ref: full label}
    "/api/calendar": lambda v, q: v.calendar(int(q.get("limit", CAL_MAX))),  # when each session ran: the calendar layout of the map
}


def make_server(root, port=8765, host="127.0.0.1"):
    """A read-only HTTP server for the page and its API (one View shared by every request). The View hides the sessions
    and the profile that opted out of visualization (novis)."""
    from .novis import Hide
    view, key, routes = View(root, hide=Hide(os.path.dirname(os.path.abspath(root)))), token(root), {**API, **EXPLORER_API}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path in ("/", "/index.html"):
                with open(PAGE, "rb") as f:
                    return self._send(200, "text/html; charset=utf-8", f.read())
            if url.path not in routes:
                return self._send(404, "application/json", b'{"error": "not found"}')
            if not hmac.compare_digest(q.get("k", ""), key):
                return self._send(403, "application/json", b'{"error": "token"}')
            try:
                body = json.dumps(routes[url.path](view, q), ensure_ascii=False).encode("utf-8")
            except KeyError as e:
                return self._send(404, "application/json", json.dumps({"error": "unknown id: %s" % e}).encode())
            except (ValueError, TypeError) as e:
                return self._send(400, "application/json", json.dumps({"error": str(e)}).encode())
            self._send(200, "application/json; charset=utf-8", body)

        def _send(self, code, kind, body):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")  # the page may sit in a sandboxed frame
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 (the base class name)
            pass

    class Server(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            view.close()

    return Server((host, port), Handler)
