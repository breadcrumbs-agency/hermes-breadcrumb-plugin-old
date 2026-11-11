"""Ingest: texts become turn crumbs; long kid lists become bounded trees.

A turn crumb points at one exact text (all its lines). A session crumb holds
its turns; when a crumb would hold more than `fanout` kids, the kids are
grouped into "span" crumbs, so every open step in a zoom stays small.
"""

import json
import time

from .crumbs import Crumb, get_crumb, put_crumb
from .lines import get_lines, put_text, split_lines


def first_line(text):
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def put_turn(store, index, role, text, session, ts=None, public=False, extra=None, kind="turn", prev=None):
    """Store one message text as a turn crumb; index it when an index is given.

    prev: the crumb this turn follows in its dialogue (linked for neighbor smoothing). A link joins a turn to a
    turn of another role: a question and its answer. Two turns of one role in a row are not linked.
    """
    data = text.encode("utf-8")
    doc = put_text(store, data)
    n = len(split_lines(data))
    meta = {"role": role, "sess": session, "ts": int(time.time() if ts is None else ts)}
    if extra:
        meta.update(extra)
    title = ("%s: %s" % (role, first_line(text)))[:60]
    cid = put_crumb(store, Crumb(kind, title, ranges=[[doc, 0, n]], public=public, meta=meta))
    if index is not None:
        index.add(cid, "%s:0-%d" % (doc, n), text, kind=kind, public=public)
        if prev and get_crumb(store, prev).meta.get("role") != role:
            index.link(prev, cid)
    return cid


def _text(content):
    """The text of a message content: a string, or the text parts of a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _calls(tool_calls):
    """[{id, name, arguments}] from OpenAI-style tool calls (a list, or its JSON text in a state.db)."""
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except ValueError:
            return []
    out = []
    for c in tool_calls or ():
        f = c.get("function") or {}
        args = f.get("arguments", "")
        out.append({"id": c.get("id") or "", "name": f.get("name") or c.get("name") or "",
                    "arguments": args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)})
    return out


# -- JSON as plain text: a crumb keeps the values, not the quotes, escapes, nulls and empty values -----------
def _one(x):
    return not isinstance(x, (dict, list)) and "\n" not in str(x)


def _str(x):
    return str(x).lower() if isinstance(x, bool) else str(x)


def json_lines(v, pad=""):
    """A JSON object or array as lines: "key: value" for each value of one line (a list of them joins with
    ", "), then each longer value under "key:" ("-" for a list item). An object or array goes two spaces in;
    a text keeps its own lines. Null and empty values drop."""
    head, body = _parts(v, pad)
    return head + body


def _parts(v, pad):
    keyed, head, body = isinstance(v, dict), [], []
    for k, x in (v.items() if keyed else enumerate(v)):
        if isinstance(x, str) and x.endswith("\n"):
            x = x[:-1]
        if x is None or x == "" or x == [] or x == {}:
            continue
        key = "%s%s:" % (pad, k) if keyed else pad + "-"
        if _one(x):
            head.append("%s %s" % (key, _str(x)))
        elif isinstance(x, list) and all(map(_one, x)):
            head.append("%s %s" % (key, ", ".join(_str(y) for y in x if y is not None)))
        elif isinstance(x, (dict, list)):
            body += [key] + json_lines(x, pad + "  ")
        else:
            body += [key] + [pad + line for line in x.split("\n")]
    return head, body


def _json(text):
    s = text.strip() if isinstance(text, str) else ""
    if s.startswith(("{", "[")):
        try:
            v = json.loads(s)
        except ValueError:
            return None
        return v if isinstance(v, (dict, list)) else None
    return None


def plain(text):
    """A JSON object or array (a tool result) as plain text: an object's first text of more than one line
    comes first, as it is (its key goes: the crumb's title is its first line), then json_lines of the rest.
    Any other text comes back as it is."""
    v = _json(text)
    if v is None:
        return text
    first = None
    if isinstance(v, dict):
        first = next((k for k, x in v.items() if isinstance(x, str) and "\n" in x.rstrip("\n")), None)
    if first is None:
        return "\n".join(json_lines(v))
    main = v[first][:-1] if v[first].endswith("\n") else v[first]
    return "\n".join([main] + json_lines({k: x for k, x in v.items() if k != first}))


def call_text(name, arguments):
    """One tool call: "\u2192 name", its arguments of one line as "key: value" on the same line (joined by
    " \u00b7 "), then its longer ones under "key:" on the lines below, two spaces in."""
    v = arguments if isinstance(arguments, (dict, list)) else _json(arguments)
    if v is None:
        return ("\u2192 %s %s" % (name, arguments or "")).rstrip()
    head, body = _parts(v, "  ")
    return "\n".join([("\u2192 %s %s" % (name, " \u00b7 ".join(h[2:] for h in head))).rstrip()] + body)


def step_text(text, calls):
    """An assistant message that calls tools: its text, then call_text of each (name, arguments)."""
    return (text.rstrip("\n") + "\n" if text.strip() else "") + "".join(call_text(n, a) + "\n" for n, a in calls)


# The tools of this memory. Their calls and results copy memory: stored in the session, never indexed
# again, because a copy ranks above its own source when a question comes back (an echo).
MEMORY_TOOLS = frozenset({"crumb", "crumb_find"})


def _memory_tool(name):
    return (name[5:] if name.startswith("mcp__") else name) in MEMORY_TOOLS


def memory_row(store, crumb):
    """True when put_rows does not index this crumb: a result of this memory's own tools, or a step that
    calls only them. A step names its tools in meta "tools"; an older step's text ends with one line per
    call: the tool name, then its arguments."""
    if crumb.kind == "tool":
        return _memory_tool(crumb.meta.get("role", "")[5:])  # role "tool:<name>"
    if crumb.kind == "step" and crumb.meta.get("tools"):
        return all(_memory_tool(t) for t in crumb.meta["tools"])
    calls = crumb.meta.get("calls") or []
    if crumb.kind != "step" or not calls or not crumb.ranges:
        return False
    lines = b"".join(get_lines(store, crumb.ranges[0][0])).decode("utf-8", "replace").splitlines()[-len(calls):]
    return len(lines) == len(calls) and all(_memory_tool(line.split(" ", 1)[0]) for line in lines)


def prune_index(store, index):
    """Drop the index rows that older versions made for crumbs that put_rows no longer indexes
    (MEMORY_TOOLS). The crumbs stay in the store. Returns (crumbs, rows) removed."""
    def text_of(ref):
        doc, _, span = ref.partition(":")
        a, _, b = span.partition("-")
        return b"".join(get_lines(store, doc, int(a or 0), int(b) if b else None)).decode("utf-8")
    found = index.db.execute("SELECT DISTINCT crumb FROM rows WHERE kind IN ('step', 'tool')").fetchall()
    crumbs = rows = 0
    for (cid,) in found:
        if memory_row(store, get_crumb(store, cid)):
            rows += index.drop(cid, text_of)
            crumbs += 1
    index.commit()
    return crumbs, rows


def put_rows(store, index, rows, session, prev=None, ts=None):
    """Store message rows in order. Returns (the new crumb ids, the last crumb of the neighbor chain):
    each indexed row is linked to the indexed row before it (prev at the start).

    rows: dicts with role and content, and optional tool_calls, tool_call_id, tool_name (or name), ts, and
      meta (more meta for a user or assistant turn, e.g. src and src_id of an import: see migrate.py).
      user or assistant text          -> a "turn" crumb
      an assistant row that calls tools -> a "step" crumb: step_text (its text, then call_text of each
                                         call); meta calls = the call ids, meta tools = the tool names
      a tool result                   -> a "tool" crumb (role "tool:<name>") of its plain() text; meta call =
                                         its call id, meta step = the step crumb that made the call
    Other rows (system, empty) are skipped. A call of this memory's own tools (MEMORY_TOOLS) and its
    result are stored but not indexed (a step is indexed when it also calls another tool)."""
    out, steps = [], {}
    for row in rows:
        role, text, calls = row.get("role"), _text(row.get("content")), _calls(row.get("tool_calls"))
        if role == "tool":
            text = plain(text)
        when = row.get("ts", ts)
        if role == "assistant" and calls:
            text = step_text(text, [(c["name"], c["arguments"]) for c in calls])
            quiet = all(_memory_tool(c["name"]) for c in calls)
            cid = put_turn(store, None if quiet else index, "assistant", text, session, when, kind="step",
                           prev=prev, extra={"calls": [c["id"] for c in calls], "tools": [c["name"] for c in calls]})
            steps.update((c["id"], cid) for c in calls if c["id"])
        elif role == "tool" and text:
            name, call = row.get("tool_name") or row.get("name") or "", row.get("tool_call_id") or ""
            extra = {"call": call} if call else {}
            if call in steps:
                extra["step"] = steps[call]
            quiet = _memory_tool(name)
            cid = put_turn(store, None if quiet else index, "tool:%s" % name if name else "tool", text, session,
                           when, kind="tool", prev=prev, extra=extra or None)
        elif role in ("user", "assistant") and text:
            quiet = False
            cid = put_turn(store, index, role, text, session, when, prev=prev, extra=row.get("meta"))
        else:
            continue
        out.append(cid)
        if not quiet:  # the neighbor chain links searchable rows only
            prev = cid
    return out, prev


def put_tree(store, kind, title, kids, fanout=16, public=False, meta=None):
    """One crumb over kids, with "span" crumbs between when kids > fanout."""
    kids = list(kids)
    if len(kids) <= fanout:
        return put_crumb(store, Crumb(kind, title, kids=kids, public=public, meta=meta or {}))
    size = fanout
    while (len(kids) + size - 1) // size > fanout:
        size *= fanout
    spans = []
    for i in range(0, len(kids), size):
        group = kids[i:i + size]
        head = get_crumb(store, group[0]).title
        spans.append(put_tree(store, "span", "%s (+%d)" % (head[:48], len(group) - 1), group, fanout, public))
    return put_crumb(store, Crumb(kind, title, kids=spans, public=public, meta=meta or {}))


def leaves(store, cid, kind=None):
    """The crumbs under cid in order (depth first): those of `kind`, or every crumb without kids."""
    out, stack = [], [cid]
    while stack:
        node_id = stack.pop()
        node = get_crumb(store, node_id)
        if node.kind == kind or (kind is None and not node.kids):
            out.append(node_id)
        stack.extend(reversed(node.kids))
    return out
