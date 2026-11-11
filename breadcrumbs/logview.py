"""The log views: SpaceGraph draws Hermes's own session log (state.db), with no store of its own.

SpaceGraph only lays out and draws. These three reads give the desktop views their data from
state.db alone, in the shapes of breadcrumbs' View, so one drawing serves both sources:

    sessions(db, limit=50)  {sessions: [{id, title, started, messages, blocks}]}, newest first
    session(db, sid)        {session, root, nodes, edges}: the messages of one session as a graph
    context(db, sid)        {session, events, turns, rows}: the Gantt (the Context of src/gantt.ts)

Read-only. Each call opens state.db with mode=ro and query_only, reads all the rows that it needs
(fetchall) and closes the connection at once, so it holds no transaction and no lock that could
block Hermes's writer. It imports only the standard library (Hermes's tracked connect is optional).

The rules, from the facts of Hermes's state.db:

- The log of a session: its rows with active = 1 or compacted = 1, in id order. A compaction
  archives the live rows (active 0, compacted 1) and inserts new live rows: the copies that it keeps
  (a protected head before its summary row, the tail after it) and one summary row
  (_compressed_summary = 1). A rewind marks rows active 0, compacted 0: they are gone.
- A copy has the display_identity of its original (Hermes's display dedupe key). The views fold
  each copy into the first row of its message: one message, one id (the lowest), one turn. A message
  is live when its last copy is live.
- Turns: the user rows that are not a summary row. The turn of a row is the last turn at or before
  it in id order. A summary row has the time of its compaction, so its turn also counts the turns
  that started before that time (the tail that its compaction kept).
- Events, in time order:
  recall    a user row whose api_content is longer than its content (UTF-8 bytes). The extra part
            is context that a provider injected (for example breadcrumbs). The view never reads
            its format: no items, bytes = the extra bytes, q = "".
  open      a session_search call in scroll shape (session_id + around_message_id) in the
            tool_calls of an assistant row: items = [around_message_id], bytes = the UTF-8 bytes of
            its tool result (the tool row with its tool_call_id), else 0.
  compress  a summary row: bytes = the UTF-8 bytes of its content, ovf true.
  Each event: id, ev, turn, ts, bytes, budget 0, ovf, rate 0.0, band None, q "", turn_budget None,
  used (the recall and open bytes of its turn so far, with the event; a compress adds 0), cap
  False, want = bytes.
- Rows: the messages of the session (bar ev "own"), then each row that an open event names (a
  message of this session keeps its own row). A bar ends at the compress that took its text out of
  the context: for an own bar, its message; for an open bar, its tool result row (the next compress
  after the event when it has none). to and t1 are None while that text is live.
- A label: "<role>[ <tool name>] <the first 40 characters of the content, white space collapsed>".
  The view is local: the label holds no more text than that.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

SCROLL_TOOL = "session_search"
LABEL_CHARS = 40
HEAD_CHARS = 1000  # the characters of a content that a label reads
MAX_SESSIONS = 500


def hermes_connect() -> Optional[Callable[..., sqlite3.Connection]]:
    """Hermes's tracked sqlite3.connect when Hermes is importable (it keeps the byte probes of
    state.db off while a reader has it open), else None (plain sqlite3.connect)."""
    try:
        from hermes_cli.sqlite_safe_read import connect_tracked
    except ImportError:
        return None
    return connect_tracked


def read(db, fn, connect=None):
    """fn(conn) on a fresh read-only connection to the state.db at path db; closed at once."""
    path = Path(db)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = "file:%s?mode=ro" % quote(str(path.resolve()))
    conn = (connect or sqlite3.connect)(uri, uri=True, timeout=2.0, check_same_thread=False)
    try:
        conn.execute("PRAGMA query_only = 1")
        return fn(conn)
    finally:
        conn.close()


class _Cols:
    """SQL for the columns of the messages table at hand (older schemas lack some of them)."""

    def __init__(self, conn):
        have = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        self.sessions = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "active" in have and "compacted" in have:
            self.visible = "(m.active = 1 OR m.compacted = 1)"
        else:
            self.visible = "m.active = 1" if "active" in have else "1"
        self.live = "m.active" if "active" in have else "1"
        self.compacted = "m.compacted" if "compacted" in have else "0"
        self.summary = "m._compressed_summary" if "_compressed_summary" in have else "0"
        self.ident = "m.display_identity" if "display_identity" in have else "NULL"
        self.api_bytes = "length(CAST(m.api_content AS BLOB))" if "api_content" in have else "NULL"
        self.tokens = "m.token_count" if "token_count" in have else "NULL"

    def select(self, where: str) -> str:
        return ("SELECT m.id, m.session_id, m.role, COALESCE(m.tool_name, ''), COALESCE(m.timestamp, 0), "
                f"{self.live}, {self.compacted}, {self.summary}, {self.ident}, "
                f"substr(COALESCE(m.content, ''), 1, {HEAD_CHARS}), length(CAST(COALESCE(m.content, '') AS BLOB)), "
                f"length(COALESCE(m.content, '')), {self.api_bytes}, COALESCE(m.tool_call_id, ''), "
                f"COALESCE(m.tool_calls, ''), {self.tokens} FROM messages m WHERE {where} ORDER BY m.id")


class Row:
    """One row of the log. The view reads sizes and the head of the content, never more text."""

    __slots__ = ("id", "session", "role", "tool", "ts", "live", "compacted", "summary", "ident", "head",
                 "nbytes", "nchars", "api_bytes", "call_id", "calls", "tokens", "gens")

    def __init__(self, r):
        (self.id, self.session, self.role, self.tool, self.ts, live, compacted, summary, self.ident, self.head,
         self.nbytes, self.nchars, self.api_bytes, self.call_id, self.calls, self.tokens) = r
        self.role = self.role or ""
        self.ts = float(self.ts or 0)
        self.live, self.compacted, self.summary = bool(live), bool(compacted), bool(summary)
        self.gens = [self]  # this row, then its copies (id order)

    @property
    def label(self) -> str:
        return label(self.role, self.tool, self.head)

    @property
    def own_bytes(self) -> int:
        """The bytes of the row's own text: its content, and the tool calls of an assistant row."""
        return (self.nbytes or 0) + len(self.calls.encode("utf-8"))

    @property
    def extra_bytes(self) -> int:
        """The bytes that api_content adds to the content: context that a provider injected."""
        return max(0, (self.api_bytes or 0) - (self.nbytes or 0))


def label(role: str, tool: str, text: str) -> str:
    head = (role or "") + (" " + tool if tool else "")
    return head + " " + " ".join((text or "").split())[:LABEL_CHARS]


def tool_calls(text: str) -> list:
    """The calls of a tool_calls value (Hermes stores the provider's list as JSON): [(id, name, args)]."""
    if not text:
        return []
    try:
        calls = json.loads(text)
    except ValueError:
        return []
    out = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            out.append(("", "", None))
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            fn = call
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = None
        out.append((str(call.get("id") or call.get("call_id") or ""), str(fn.get("name") or ""),
                    args if isinstance(args, dict) else None))
    return out


def scroll_calls(text: str) -> list:
    """The session_search calls in scroll shape (session_id + around_message_id, as Hermes's
    session_search dispatches them): [(call index, call id, session id, anchor id)]."""
    if SCROLL_TOOL not in (text or ""):
        return []
    out = []
    for i, (call_id, name, args) in enumerate(tool_calls(text)):
        if name != SCROLL_TOOL or args is None:
            continue
        sid, anchor = args.get("session_id"), args.get("around_message_id")
        if not isinstance(sid, str) or not sid.strip() or anchor is None or isinstance(anchor, bool):
            continue
        try:
            anchor = int(anchor)
        except (TypeError, ValueError):
            continue
        out.append((i, call_id, sid.strip(), anchor))
    return out


def _fold(rows: list) -> list:
    """The messages of a session: each copy that a compaction made is folded into its first row."""
    first, out = {}, []
    for r in rows:
        if r.ident is not None and r.ident in first:
            first[r.ident].gens.append(r)
            continue
        if r.ident is not None:
            first[r.ident] = r
        out.append(r)
    return out


def _session_rows(conn, cols: _Cols, sid: str) -> list:
    rows = [Row(r) for r in conn.execute(cols.select(f"m.session_id = ? AND {cols.visible}"), (sid,)).fetchall()]
    if not rows:
        known = conn.execute("SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (sid,)).fetchall() or \
            conn.execute("SELECT 1 FROM messages WHERE session_id = ? LIMIT 1", (sid,)).fetchall()
        if not known:
            raise KeyError(sid)
    return rows


class _Log:
    """One session's log: its messages, turns, summary rows and the turn of each row."""

    def __init__(self, rows: list):
        self.rows = rows  # every visible row, id order
        self.messages = _fold(rows)
        self.message_of = {g.id: m for m in self.messages for g in m.gens}
        self.turns = [m for m in self.messages if m.role == "user" and not m.summary]
        self.turn_ids = [m.id for m in self.turns]
        times, top = [], float("-inf")
        for m in self.turns:  # the start time of each turn, never earlier than the turn before it
            top = max(top, m.ts)
            times.append(top)
        self.turn_times = times
        self.stops = [m.id for m in self.messages if m.summary]  # one summary row for each compaction, id order
        self.results = {}  # tool_call_id -> the tool rows that answer it, id order
        for r in rows:
            if r.role == "tool" and r.call_id:
                self.results.setdefault(r.call_id, []).append(r)

    def turn_of(self, row: Row) -> int:
        t = bisect.bisect_right(self.turn_ids, row.id) - 1
        if row.summary:  # placed at the time of its compaction (after the tail that it kept)
            t = max(t, bisect.bisect_right(self.turn_times, row.ts) - 1)
        return max(t, 0)

    def end(self, m: Row) -> Optional[int]:
        """The index into stops of the compress that took message m out of the context; None while
        it is live. A compaction archives every live row, so each copy of a message left the
        context at a later compress than the copy before it."""
        k = -1
        for g in m.gens:
            if g.live:
                return None
            k = next((j for j in range(k + 1, len(self.stops)) if self.stops[j] > g.id), None)
            if k is None:
                return None
        return k if k >= 0 else None

    def tool_result(self, after: int, call_id: str) -> Optional[Row]:
        """The first tool row after row id `after` that answers call_id."""
        return next((r for r in self.results.get(call_id, ()) if r.id > after), None)


def _event(eid, ev, turn, ts, nbytes, ovf=False) -> dict:
    return {"id": eid, "ev": ev, "turn": turn, "ts": ts, "bytes": nbytes, "budget": 0, "ovf": ovf, "rate": 0.0,
            "band": None, "q": "", "turn_budget": None, "used": 0, "cap": False, "want": nbytes}


def _bar(start, stop, t0, t1, nbytes, ev) -> dict:
    return {"from": start, "to": stop, "t0": t0, "t1": t1, "level": 1.0, "bytes": nbytes, "ev": ev}


def context(db, sid: str, connect=None) -> dict:
    """The Gantt of a session's context, from state.db alone (src/gantt.ts Context)."""
    def run(conn):
        cols = _Cols(conn)
        log = _Log(_session_rows(conn, cols, sid))
        # (sort key, event, items, the row whose text the event put into the context)
        found = []
        for m in log.turns:
            if m.extra_bytes:
                found.append(((m.ts, m.id, 0), _event("recall-%d" % m.id, "recall", log.turn_of(m), m.ts,
                                                      m.extra_bytes), [], None))
        for m in log.messages:
            if m.role == "assistant":
                for i, call_id, target_sid, anchor in scroll_calls(m.calls):
                    result = log.tool_result(m.id, call_id) if call_id else None
                    nbytes = (result.nbytes or 0) if result else 0
                    found.append(((m.ts, m.id, i), _event("open-%d-%d" % (m.id, i), "open", log.turn_of(m), m.ts,
                                                         nbytes), [(anchor, target_sid)], result))
            if m.summary:
                found.append(((m.ts, m.id, 0), _event("compress-%d" % m.id, "compress", log.turn_of(m), m.ts,
                                                      m.nbytes or 0, ovf=True), [], None))
        found.sort(key=lambda f: f[0])
        events = [f[1] for f in found]
        spent = {}
        for e in events:
            if e["ev"] in ("recall", "open"):
                spent[e["turn"]] = spent.get(e["turn"], 0) + e["bytes"]
            e["used"] = spent.get(e["turn"], 0)
        stop_event = {key[1]: i for i, (key, e, _, _) in enumerate(found) if e["ev"] == "compress"}  # row id -> index

        def until(k):  # (event index, turn) of the k-th summary row, or (None, None)
            i = None if k is None else stop_event.get(log.stops[k])
            return (None, None) if i is None else (i, events[i]["turn"])

        rows = {}
        for m in log.messages:
            to, t1 = until(log.end(m))
            rows[m.id] = {"id": str(m.id), "kind": m.role, "label": m.label, "session": m.session,
                          "bars": [_bar(None, to, log.turn_of(m), t1, m.own_bytes, "own")]}
        named = {anchor for f in found for anchor, _ in f[2] if log.message_of.get(anchor) is None}
        other = {}
        if named:
            marks = ",".join("?" * len(named))
            other = {r.id: r for r in (Row(x) for x in conn.execute(cols.select(f"m.id IN ({marks})"),
                                                                     sorted(named)).fetchall())}
        for i, (_, e, items, result) in enumerate(found):
            if not items:
                continue
            if result is not None:
                to, t1 = until(log.end(log.message_of[result.id]))
            else:
                to = next((j for j in range(i + 1, len(events)) if events[j]["ev"] == "compress"), None)
                t1 = None if to is None else events[to]["turn"]
            for anchor, call_sid in items:
                mine = log.message_of.get(anchor)
                key = mine.id if mine is not None else anchor
                if key not in rows:
                    r = other.get(anchor)
                    rows[key] = ({"id": str(r.id), "kind": r.role, "label": r.label, "session": r.session, "bars": []}
                                 if r is not None else
                                 {"id": str(anchor), "kind": "", "label": "", "session": call_sid, "bars": []})
                rows[key]["bars"].append(_bar(i, to, e["turn"], t1, e["bytes"] // len(items), e["ev"]))
        turns = [{"turn": i, "crumb": str(m.id), "ts": m.ts} for i, m in enumerate(log.turns)]
        return {"session": sid, "events": events, "turns": turns, "rows": list(rows.values())}

    return read(db, run, connect)


def session(db, sid: str, connect=None) -> dict:
    """One session as a graph: the messages as nodes; nbr edges (a message -> the one before it) and
    call edges (a tool row -> the assistant row whose tool_calls hold its tool_call_id)."""
    def run(conn):
        return _Log(_session_rows(conn, _Cols(conn), sid))

    log = read(db, run, connect)
    nodes, edges = [], []
    callers = {}  # call id -> [(row id, message id)] of the assistant rows that hold it, id order
    for r in log.rows:
        if r.role == "assistant":
            for call_id, _, _ in tool_calls(r.calls):
                if call_id:
                    callers.setdefault(call_id, []).append((r.id, log.message_of[r.id].id))
    for i, m in enumerate(log.messages):
        nodes.append({"id": str(m.id), "kind": m.role, "title": m.label, "tokens": m.tokens or (m.nchars or 0) // 4,
                      "meta": {"tool_name": m.tool or None, "ts": m.ts, "compacted": m.gens[-1].compacted,
                               "summary": m.summary, "turn": log.turn_of(m)}})
        if i:
            edges.append({"kind": "nbr", "source": str(m.id), "target": str(log.messages[i - 1].id), "w": 1.0})
        if m.role == "tool" and m.call_id in callers:
            held = callers[m.call_id]
            before = [mid for rid, mid in held if rid < m.id]
            target = before[-1] if before else held[0][1]
            if target != m.id:
                edges.append({"kind": "call", "source": str(m.id), "target": str(target), "w": 1.0})
    return {"session": sid, "root": str(log.messages[0].id) if log.messages else None, "nodes": nodes, "edges": edges}


def sessions(db, limit=50, connect=None) -> dict:
    """The sessions, newest first: id, title, started, messages (the messages of its log) and
    blocks (its user rows whose api_content adds context: the recall events of its Gantt)."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("limit must be an integer from 1 to %d, not %r" % (MAX_SESSIONS, limit))
    if not 1 <= limit <= MAX_SESSIONS:
        raise ValueError("limit must be an integer from 1 to %d, not %r" % (MAX_SESSIONS, limit))

    def run(conn):
        cols = _Cols(conn)
        title = "COALESCE(s.title, '')" if "title" in cols.sessions else "''"
        started = "COALESCE(s.started_at, 0)" if "started_at" in cols.sessions else "0"
        each = f"COUNT(DISTINCT COALESCE({cols.ident}, m.id)) FROM messages m WHERE m.session_id = s.id AND {cols.visible}"
        blocks = (f"{each} AND m.role = 'user' AND NOT {cols.summary} "
                  f"AND {cols.api_bytes} > length(CAST(COALESCE(m.content, '') AS BLOB))")
        return conn.execute(f"SELECT s.id, {title}, {started}, (SELECT {each}), (SELECT {blocks}) FROM sessions s "
                            f"ORDER BY {started} DESC, s.id DESC LIMIT ?", (limit,)).fetchall()

    return {"sessions": [{"id": r[0], "title": r[1], "started": r[2], "messages": r[3], "blocks": r[4]}
                         for r in read(db, run, connect)]}
