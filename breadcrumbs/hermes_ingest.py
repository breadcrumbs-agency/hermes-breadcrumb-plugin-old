"""Backfill: read a Hermes state.db (opened read-only) into a breadcrumbs store.

Each session becomes a session crumb (ref "sess-<id>") over its messages in
order: user and assistant rows as "turn" crumbs, tool rows as "tool" crumbs.
Each turn is linked to the one before it (neighbor smoothing in search).
Running it twice adds only a new commit: blobs and index rows dedupe.
A session row's parent_session_id and source go into the session crumb's meta, so a child session names
its parent. A database whose sessions table lacks a column ingests as before, without that key.
"""

import os
import sqlite3
import time

from .crumbs import get_crumb
from .hermes import _ref_name
from .history import commit
from .ingest import put_rows, put_tree
from .runtime import no_gc

ROLES = ("user", "assistant", "tool")
SESSION_META = ("parent_session_id", "source")  # sessions columns that the session crumb keeps in its meta


def _session(store, index, refs, con, sid, title, roles, stats, extra=None):
    have = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    cols = ", ".join(c if c in have else "NULL" for c in ("tool_calls", "tool_call_id"))
    rows = con.execute("SELECT role, content, tool_name, timestamp, %s FROM messages"
                       " WHERE session_id = ? ORDER BY id" % cols, (sid,)).fetchall()
    keep = []
    for role, content, tool_name, ts, calls, call_id in rows:
        if role not in roles or not (content or (role == "assistant" and calls)):
            continue
        text = content if isinstance(content, str) else ("" if content is None else str(content))
        keep.append({"role": role, "content": text, "tool_name": tool_name, "ts": int(ts or 0),
                     "tool_calls": calls, "tool_call_id": call_id})
        stats["messages"] += 1
        stats["bytes"] += len(text.encode("utf-8"))
    turns, _ = put_rows(store, index, keep, sid)
    if not turns:
        return None
    sess = put_tree(store, "sess", title or get_crumb(store, turns[0]).title, turns, meta=dict(extra or {}, sess=sid))
    refs.set(_ref_name("sess-", sid), sess)
    stats["sessions"] += 1
    return sess


def ingest_state_db(db_path, store, index, refs, roles=ROLES):
    con = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True)
    t0 = time.perf_counter()
    stats = {"sessions": 0, "messages": 0, "bytes": 0}
    roots = []
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
        cols = ", ".join(c if c in have else "NULL" for c in SESSION_META)
        with no_gc():  # bulk backfill: skip the cyclic GC scans
            for sid, title, *kin in con.execute("SELECT id, COALESCE(title, ''), %s FROM sessions"
                                                " ORDER BY started_at, id" % cols).fetchall():
                extra = {k: str(v) for k, v in zip(SESSION_META, kin) if v}  # NULL and empty: no key
                sess = _session(store, index, refs, con, sid, title, roles, stats, extra)
                if sess:
                    roots.append(sess)
    finally:
        con.close()
    if roots:
        commit(store, refs, "main", roots, "ingest " + os.path.basename(db_path))
    store.flush()
    index.commit()
    stats["seconds"] = round(time.perf_counter() - t0, 3)
    return stats
