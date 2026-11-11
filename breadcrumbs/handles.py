"""Handles: the id of a child's work, in the result of the delegate_task call that made the child.

A delegate_task child runs with no memory provider. With the dial `family` on, the lead's provider keeps the work of
the child (hermes.py, on_delegation: the child's CAR comes into the store of the lead as private crumbs). The model
that delegated must also learn the id of that work, or it cannot open it. The one place where it reads about the child
is the result of the delegate_task call. Hermes lets a plugin replace a tool result before the model reads it (the
hook transform_tool_result). So there are two steps:

  1. on_delegation calls offer(): the handle line of one child. Hermes calls on_delegation once for each child that ran,
     before the result of the call exists.
  2. transform_tool_result() puts each line in the entry of its child in the result, under the key KEY.

The result stays valid JSON, and only the entries of children change. Hermes judges a result that parses by its
top level (agent/display.py, _detect_tool_failure). A result that does not parse goes through a text test of its first
500 characters, where a child with "status": "failed" would turn the whole call into a failed tool call. So the lines
are not added after the JSON text.

A line belongs to an entry when the session, the goal of the task and the time agree. Hermes gives on_delegation the
goal of the task (not its index), and the hook gets the arguments of the call, so the goal of entry i is the goal that
the offer carries. A task that Hermes refused before it ran has an entry and no offer. So the lines of one goal are used
only when the offers for that goal are as many as the tasks with that goal; they then go to the tasks in order. An offer
that is older than the call is not used. A task that does not match gets no line: a wrong handle would send the model to
the wrong child, and a missing line costs only a crumb_find. (Hermes scrubs secrets from the goal before on_delegation
sees it. A goal with a secret in it differs from the goal in the arguments, so its task gets no line either.)

Hermes runs a delegate_task call to its end, and returns the entries, in a subagent, in a gateway session with
delegation.gateway_mode inline, and in a session that cannot receive a detached result (one-shot, an API request, a
full pool: tools/delegate_tool_dispatch.py, _resolve_async_wake_sid). In an interactive session the call detaches its
children and returns a dispatch handle with no entries; the summaries come back later as a message, which no plugin
hook sees. There the hook changes nothing, and the offers wait for a result that never comes, until they expire
(LIFETIME). The import and the worker receipt do not depend on it: they happen in on_delegation.

The offers are state of the process, shared by every provider in it, so each offer names its session: a line is never
shown to another chat. A line that no result takes expires.
"""

import collections
import json
import threading
import time

KEY = "breadcrumbs"  # the key that the entry of a child gets in the result of delegate_task
LIFETIME = 600.0  # seconds that an offer may wait for its result, when Hermes does not say how long the call took
SLACK = 5.0  # seconds added to the time of the call

_offers = collections.deque(maxlen=256)  # (monotonic time, session id, goal, handle line), oldest first
_lock = threading.Lock()


def offer(session_id, goal, line):
    """The handle line of one child, from on_delegation. line is "" when the work of the child was not kept: the offer
    then only holds the place of the child among the offers of the same goal."""
    now = time.monotonic()
    with _lock:
        while _offers and now - _offers[0][0] > LIFETIME:
            _offers.popleft()
        _offers.append((now, session_id or "", goal or "", line or ""))


def goals(args):
    """The goal of each task of a delegate_task call, in task order: the `tasks` list when it has entries, else the
    one `goal` (the same choice as Hermes's _normalize_task_list). An entry that is no goal is None."""
    if not isinstance(args, dict):
        return []
    tasks = args.get("tasks")
    if isinstance(tasks, str):  # Hermes also reads a list that the model sent as JSON text
        try:
            tasks = json.loads(tasks)
        except ValueError:
            tasks = None
    if isinstance(tasks, list) and tasks:
        return [t.get("goal") if isinstance(t, dict) and isinstance(t.get("goal"), str) else None for t in tasks]
    goal = args.get("goal")
    return [goal if isinstance(goal, str) else None]


def _claim(session_id, wanted, horizon):
    """Take the offers of this call out of the queue: those of this session, not older than horizon, for a goal that
    the call has. Returns {goal: [line, ...]} in the order of the offers. An offer is claimed whether or not a task
    uses its line."""
    want = {g for g in wanted if g is not None}
    mine = [o for o in _offers if o[0] >= horizon and o[2] in want and o[1] == session_id]
    for o in mine:
        _offers.remove(o)
    out = {}
    for _, _, goal, line in mine:
        out.setdefault(goal, []).append(line)
    return out


def transform_tool_result(*, tool_name="", args=None, result=None, session_id="", duration_ms=0, **_):
    """The hook transform_tool_result. Returns the result of a delegate_task call with the handle line of each child in
    the entry of that child, or None (Hermes then keeps the result as it is) for every other tool, for a result that
    has no entries yet (a call that runs in the background), and when no line matches. It never raises."""
    try:
        if tool_name != "delegate_task" or not _offers or not isinstance(result, str):
            return None
        try:
            data = json.loads(result)
        except ValueError:
            return None
        entries = data.get("results") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            return None
        wanted = goals(args)
        horizon = time.monotonic() - ((duration_ms / 1000.0 + SLACK) if duration_ms else LIFETIME)
        with _lock:
            lines = _claim(session_id, wanted, horizon)
        by_index = {e["task_index"]: e for e in entries if isinstance(e, dict) and type(e.get("task_index")) is int}
        changed = False
        for goal, offered in lines.items():
            tasks = [i for i, g in enumerate(wanted) if g == goal]
            if len(tasks) != len(offered):  # a task that did not run, or a child that this call does not own
                continue
            for i, line in zip(tasks, offered):
                if line and i in by_index:
                    by_index[i][KEY] = line
                    changed = True
        return json.dumps(data, ensure_ascii=False) if changed else None  # delegate_task writes it the same way
    except Exception:  # a hook never breaks a tool result
        return None
