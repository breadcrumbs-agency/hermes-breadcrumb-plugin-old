"""Family: the session of a delegate_task child, from Hermes's state.db to its own store and a CAR file.

A delegate_task child runs with no memory provider, so none of its turns become crumbs. The parent's provider
hears of the child once, at on_delegation. With the dial `family` on, it calls export_child there:

  1. It reads the child's rows from <hermes_home>/state.db, read-only (logview.read: mode=ro and query_only,
     with Hermes's own tracked connect when Hermes can be imported). It reads only the rows of that child, in
     one pass, and closes state.db at once: the store is written from an in-memory copy of those rows, so no
     connection to state.db stays open while the store is written.
  2. It writes them to the child's own store, <breadcrumbs root>/children/<child sid>/. That folder has the
     layout of any breadcrumbs home (store/, index.db, refs/), so `python -m breadcrumbs --home FOLDER verify`
     works on it. The rows become the crumbs that `ingest-hermes` makes (hermes_ingest._session).
  3. It makes a public copy of the session (share.publish, deep) and exports that copy as FOLDER/share/<sid>.car.
     The private crumbs of the child's store are not in the file.

The parent's own store is not touched here: importing the CAR is the next step. The CAR file is the transport,
and nothing leaves the machine. A child with no rows that a store keeps leaves nothing behind.
"""

import contextlib
import os
import re
import shutil
import sqlite3

from .hermes_ingest import ROLES, _session
from .history import Refs
from .index import Index
from .logview import hermes_connect, read
from .share import export_car, publish
from .store import Store

SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")  # a session id names a folder: no path, no leading dot
COLS = ("id", "session_id", "role", "content", "tool_name", "timestamp", "tool_calls", "tool_call_id")  # what _session reads


def child_home(root, sid):
    """The folder of a child's own store (under the breadcrumbs folder root), or None when sid is no plain
    folder name."""
    return os.path.join(root, "children", sid) if SID.fullmatch(sid or "") else None


def export_child(db_path, root, sid, connect=None):
    """Read session sid from the state.db at db_path (read-only) into its own store, and write its CAR file.

    Returns {sid, home, car, cid, root, messages, blocks, bytes}: the folder of the store, the path of the CAR,
    its root CID, the id of the public root crumb in the child's store, the rows kept, and the blocks and bytes
    of the CAR. Returns None when sid is no plain folder name or state.db has no row of it that a store keeps
    (the call then leaves no folder that it made). FileNotFoundError (no state.db) and sqlite3.Error (it cannot
    be read) go to the caller.
    """
    home = child_home(root, sid)
    if home is None:
        return None
    staged = read(db_path, lambda conn: _stage(conn, sid), connect or hermes_connect())  # state.db is closed here
    if staged is None:
        return None
    title, rows = staged
    existed, got = os.path.isdir(home), None
    try:
        got = _build(home, sid, title, rows)
        return got
    finally:
        rows.close()
        if got is None and not existed:  # a build that failed or kept nothing leaves no folder that this call made
            shutil.rmtree(home, ignore_errors=True)
            with contextlib.suppress(OSError):
                os.rmdir(os.path.dirname(home))  # children/, only when it is empty


def _stage(conn, sid):
    """(title, an in-memory database that holds the rows of session sid in the shape that hermes_ingest._session
    reads), or None when the session has no row that a store keeps. The caller closes the database."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    cols = ", ".join(c if c in have else "NULL" for c in COLS)  # an older state.db may lack tool_calls and tool_call_id
    rows = conn.execute("SELECT %s FROM messages WHERE session_id = ? AND role IN (%s) ORDER BY id"
                        % (cols, ",".join("?" * len(ROLES))), (sid,) + ROLES).fetchall()
    if not any(r[3] or (r[2] == "assistant" and r[6]) for r in rows):  # the rows that _session keeps
        return None
    title = ""
    if "title" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}:
        row = conn.execute("SELECT COALESCE(title, '') FROM sessions WHERE id = ?", (sid,)).fetchone()
        title = row[0] if row else ""
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE messages (%s)" % ", ".join(COLS))  # no column types: a value stays as it was
    mem.executemany("INSERT INTO messages VALUES (%s)" % ",".join("?" * len(COLS)), rows)
    return title, mem


def _build(home, sid, title, rows):
    store, index = Store(os.path.join(home, "store")), Index(os.path.join(home, "index.db"))
    try:
        stats = {"sessions": 0, "messages": 0, "bytes": 0}
        sess = _session(store, index, Refs(os.path.join(home, "refs")), rows, sid, title, ROLES, stats)
        if sess is None:  # put_rows kept no turn (every kept row had no text)
            return None
        top = publish(store, sess, deep=True)  # the CAR holds public crumbs only; the private ones stay here
        store.flush()
        index.commit()
        car = os.path.join(home, "share", sid + ".car")
        os.makedirs(os.path.dirname(car), exist_ok=True)
        roots, out = export_car(store, [top], car)
    finally:
        index.close()
        store.close()
    return {"sid": sid, "home": home, "car": car, "cid": roots[0], "root": top, "messages": stats["messages"],
            "blocks": out["blocks"], "bytes": out["bytes"]}
