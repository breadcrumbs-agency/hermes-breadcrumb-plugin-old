"""migrate: other memory systems' exports become crumbs that keep their source ids.

A conversation becomes a session (ref sess-<src>:<conversation id>) over its messages in order, as a
Hermes session does (hermes_ingest.py). A memory record becomes a "mem" crumb in one session per source
(<src>:memories). Every crumb keeps meta src (the system) and src_id (its id there). A record without a
time takes the time of the export file. A second run adds only a commit on the ref "import": the crumbs
and the index rows are the same.

  chatgpt   conversations.json of an OpenAI data export: the visible branch of each conversation
  claude    conversations.json of a claude.ai data export
  hermes    MEMORY.md or USER.md of a Hermes profile: entries split by a line with "§"
  mem0, openmemory, supermemory, hindsight, honcho, retaindb
            a JSON list of memory records as the system's list API gives it, or an object that holds the
            list ("results", "items", "memories", "memoryEntries" or "data"). The text is the first of
            "memory", "content", "text"; the id is "id" or "uuid"; the time "created_at", "createdAt" or
            "date". Small id fields (user, agent, run, session, peer, app, type) stay in meta.
"""
import datetime
import json
import os

from .hermes import _ref_name
from .history import commit
from .ingest import put_rows, put_tree, put_turn

SYSTEMS = ("chatgpt", "claude", "hermes", "mem0", "openmemory", "supermemory", "hindsight", "honcho", "retaindb")
LISTS = ("results", "items", "memories", "memoryEntries", "data")
TEXT, IDS, TIMES = ("memory", "content", "text"), ("id", "uuid"), ("created_at", "createdAt", "date")
KEEP = ("user_id", "agent_id", "run_id", "session_id", "peer_id", "app_id", "type", "memory_type", "updated_at")
DELIMITER = "\n\u00a7\n"  # Hermes's memory files (tools/memory_tool_store.py ENTRY_DELIMITER)


def _time(value):
    """Seconds from an epoch number or an ISO 8601 text; None when absent or unreadable."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return None


def chatgpt(data):
    """(conversation id, title, rows): the branch from current_node up to the root, oldest first."""
    for conv in data if isinstance(data, list) else []:
        mapping, node, path = conv.get("mapping") or {}, conv.get("current_node"), []
        while node in mapping and len(path) < len(mapping):  # the length guards against a cycle
            path.append(mapping[node])
            node = mapping[node].get("parent")
        rows = []
        for n in reversed(path):
            m = n.get("message") or {}
            role = (m.get("author") or {}).get("role")
            text = "\n".join(p for p in (m.get("content") or {}).get("parts") or [] if isinstance(p, str))
            if role in ("user", "assistant") and text.strip():
                rows.append({"role": role, "content": text, "ts": _time(m.get("create_time")),
                             "src_id": m.get("id") or n.get("id")})
        yield conv.get("conversation_id") or conv.get("id"), conv.get("title") or "", rows


def claude(data):
    """(conversation id, title, rows) for each conversation of a claude.ai export."""
    for conv in data if isinstance(data, list) else []:
        rows = []
        for m in conv.get("chat_messages") or []:
            text = m.get("text") or "\n".join(c.get("text", "") for c in m.get("content") or []
                                              if isinstance(c, dict) and c.get("type") == "text")
            role = {"human": "user", "assistant": "assistant"}.get(m.get("sender"))
            if role and text.strip():
                rows.append({"role": role, "content": text, "ts": _time(m.get("created_at")), "src_id": m.get("uuid")})
        yield conv.get("uuid"), conv.get("name") or "", rows


def hermes_entries(text, name):
    """The entries of a Hermes memory file; the id is the file name and the entry's number."""
    entries = [e.strip("\n") for e in text.split(DELIMITER) if e.strip()]
    return [{"src_id": "%s#%d" % (name, i), "text": e} for i, e in enumerate(entries, 1)]


def records(data):
    """Memory records from a JSON list (or an object that holds one), with the keys of the module doc."""
    if isinstance(data, dict):
        data = next((data[k] for k in LISTS if isinstance(data.get(k), list)), [])
    for r in data if isinstance(data, list) else []:
        if not isinstance(r, dict):
            continue
        text = next((r[k] for k in TEXT if isinstance(r.get(k), str) and r[k].strip()), None)
        rid = next((str(r[k]) for k in IDS if r.get(k) not in (None, "")), None)
        if text and rid:
            yield {"src_id": rid, "text": text, "ts": _time(next((r[k] for k in TIMES if r.get(k)), None)),
                   "meta": {k: r[k] for k in KEEP if isinstance(r.get(k), (str, int)) and len(str(r[k])) <= 120}}


def _session(store, refs, sid, title, kids):
    sess = put_tree(store, "sess", title[:60], kids, meta={"sess": sid})
    refs.set(_ref_name("sess-", sid), sess)
    return sess


def migrate(store, index, refs, src, conversations=(), memories=(), ts=0):
    """Store conversations ((id, title, rows)) and memory records from src; commit on "import"."""
    stats, roots = {"sessions": 0, "messages": 0, "memories": 0}, []
    for conv_id, title, rows in conversations:
        sid = "%s:%s" % (src, conv_id)
        rows = [dict(r, ts=r.get("ts") or ts, meta={"src": src, "src_id": str(r["src_id"])}) for r in rows]
        turns, _ = put_rows(store, index, rows, sid)
        if turns:
            roots.append(_session(store, refs, sid, title or sid, turns))
            stats["sessions"] += 1
            stats["messages"] += len(turns)
    mems = [put_turn(store, index, "memory", r["text"], "%s:memories" % src, r.get("ts") or ts,
                     extra=dict(r.get("meta") or {}, src=src, src_id=r["src_id"]), kind="mem") for r in memories]
    if mems:
        roots.append(_session(store, refs, "%s:memories" % src, "%s memories" % src, mems))
        stats["memories"] = len(mems)
    if roots:
        commit(store, refs, "import", roots, "migrate " + src)
    store.flush()
    index.commit()
    return stats


def migrate_file(store, index, refs, system, path):
    """Read one export file of system (see SYSTEMS) into the store. Returns counts."""
    ts = int(os.path.getmtime(path))
    with open(path, encoding="utf-8") as f:
        if system == "hermes":
            return migrate(store, index, refs, system, memories=hermes_entries(f.read(), os.path.basename(path)), ts=ts)
        data = json.load(f)
    if system in ("chatgpt", "claude"):
        return migrate(store, index, refs, system, conversations=(chatgpt if system == "chatgpt" else claude)(data), ts=ts)
    return migrate(store, index, refs, system, memories=records(data), ts=ts)
