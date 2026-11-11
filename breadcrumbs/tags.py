"""Tags: a judgment on a crumb, kept apart from the crumb that it judges.

A tag is a crumb of kind "tag". Its title is the value. Its heritage holds the subject crumb, then the
object crumb. `supersede` writes "OLD is superseded by NEW": title "superseded", heritage [OLD, NEW].
meta {"neg": 1} retracts an earlier tag, as `neg` does in AT Protocol. A tag holds no text, no time and
no author, so the same tag is the same crumb. A tag is private (no `v`).

The ref "tags" records each write in order (one commit for each tag). The index table `tags` holds the
current state. It is derived data: replay() makes it again from the commits. It has a row for each key of
the old crumb: its id, and the ref "doc:a-b" of each of its ranges. An ACE layer names a text by that ref.
"""

import sqlite3

from .crumbs import Crumb, get_crumb, is_crumb, put_crumb
from .history import commit, log
from .lines import encode_spine, split_lines

SUPERSEDED = "superseded"
REF = "tags"
CHAIN = 32  # the longest chain of successors that successors() follows
_PART = 500  # ids in one SQL IN list


def resolve(store, index, ref):
    """The crumb id of ref: a crumb id, or a text ("doc" or "doc:a-b") that a crumb holds."""
    doc = str(ref).partition(":")[0]
    try:
        blob = store.get(doc)
    except (KeyError, ValueError):
        raise KeyError(ref) from None
    if ":" not in ref and is_crumb(blob):
        return ref
    held = index.holder(doc)
    if held is None:
        raise KeyError(ref)
    return held


def keys(store, crumb_id):
    """The keys of a crumb in the table `tags`: its id, then the ref of each of its ranges."""
    return [crumb_id] + ["%s:%d-%d" % (d, a, b) for d, a, b in get_crumb(store, crumb_id).ranges]


def _next(db, refs):
    """{key: the newest live successor} for the keys that have one."""
    out = {}
    for i in range(0, len(refs), _PART):
        part = refs[i:i + _PART]
        for key, new in db.execute("SELECT key, new FROM tags WHERE key IN (%s) ORDER BY n"
                                   % ",".join("?" * len(part)), part).fetchall():
            out[key] = new  # ORDER BY n: the newest tag comes last and wins
    return out


def chain(index, crumb_id):
    """The successors of a crumb, in order (the newest last)."""
    out, cur = [], crumb_id
    for _ in range(CHAIN):
        cur = _next(index.db, [cur]).get(cur)
        if cur is None or cur in out or cur == crumb_id:
            break
        out.append(cur)
    return out


def successors(index, refs):
    """{ref: the newest crumb that supersedes it} for the refs (crumb ids or "doc:a-b") that have one.
    A text ref whose newest successor holds the same text is current again, so it is not in the result."""
    refs = list(dict.fromkeys(refs))
    if index is None or not refs:
        return {}
    try:
        out = _next(index.db, refs)
        cur = dict(out)
        for _ in range(CHAIN):
            more = _next(index.db, list(set(cur.values())))
            cur = {r: more[n] for r, n in cur.items() if n in more and more[n] != r}
            if not cur:
                break
            out.update(cur)
        for r in [r for r in out if ":" in r]:
            if index.db.execute("SELECT 1 FROM rows WHERE crumb = ? AND ref = ?", (out[r], r)).fetchone():
                del out[r]
        return {r: n for r, n in out.items() if n != r}
    except sqlite3.OperationalError:  # an index from before tags: a reader cannot make the table
        return {}


def _apply(index, store, crumb, n):
    old, new = crumb.heritage
    if crumb.meta.get("neg"):
        index.db.execute("DELETE FROM tags WHERE old = ? AND new = ?", (old, new))
    else:
        index.db.executemany("INSERT OR REPLACE INTO tags VALUES (?, ?, ?, ?)",
                             [(k, old, new, n) for k in keys(store, old)])


def supersede(store, index, refs, old, new, undo=False, ts=None):
    """Write the tag "old is superseded by new" (undo: its neg tag) and commit it on the ref "tags".
    old and new are crumb ids or texts that crumbs hold. Returns the id of the tag crumb."""
    old, new = resolve(store, index, old), resolve(store, index, new)
    live = index.db.execute("SELECT 1 FROM tags WHERE old = ? AND new = ? LIMIT 1", (old, new)).fetchone()
    if undo and not live:
        raise ValueError("%s is not superseded by %s" % (old, new))
    if not undo and (old == new or old in chain(index, new)):
        raise ValueError("a cycle: %s comes after %s" % (old, new))
    tag = put_crumb(store, Crumb("tag", SUPERSEDED, heritage=[old, new], meta={"neg": 1} if undo else {}))
    commit(store, refs, REF, [tag], "%s%s %s %s" % ("undo " if undo else "", SUPERSEDED, old, new), ts=ts)
    store.flush()
    n = index.db.execute("SELECT coalesce(max(n), 0) + 1 FROM tags").fetchone()[0]
    _apply(index, store, get_crumb(store, tag), n)
    index.commit()
    return tag


def replay(store, index, refs):
    """Make the table `tags` again from the commits of the ref "tags", oldest first."""
    index.db.execute("DELETE FROM tags")
    head = refs.get(REF)
    writes = [c.kids for _, c in log(store, head)] if head else []
    for n, kids in enumerate(reversed(writes), 1):
        for tag in kids:
            c = get_crumb(store, tag)
            if c.kind == "tag" and c.title == SUPERSEDED and len(c.heritage) == 2:
                _apply(index, store, c, n)
    index.commit()


def holders(store, index, text, kind=None):
    """The crumbs that hold exactly this text (one range of all its lines), oldest first. It writes nothing."""
    lines = split_lines(text.encode("utf-8"))
    ids = []
    for line in lines:
        blob_id = store.find(line)
        if blob_id is None:
            return []
        ids.append(blob_id)
    doc = store.find(encode_spine(ids))
    if doc is None:
        return []
    sql, args = "SELECT crumb FROM rows WHERE ref = ?", ["%s:0-%d" % (doc, len(lines))]
    if kind:
        sql, args = sql + " AND kind = ?", args + [kind]
    return [c for (c,) in index.db.execute(sql + " ORDER BY id", args).fetchall()]


def handle(zoom, old, new):
    """A superseded crumb as one line: its id, its kind and its size, no text, then its successor."""
    kind = get_crumb(zoom.store, old).kind
    return "\u25b8 %s %s \u00b7%dt superseded by %s\n" % (old, kind, zoom.full(old), new)
