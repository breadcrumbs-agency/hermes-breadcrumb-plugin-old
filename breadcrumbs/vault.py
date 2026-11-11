"""vault: the memory as an Obsidian vault (export), and an Obsidian vault as crumbs (import).

export writes one note per session, sessions/NAME.md: YAML frontmatter (the crumb id, the session id,
the time of its newest turn in UTC, the number of turns, the cue words as tags), then each turn with its
crumb id and its exact text in a code fence (so the Markdown in a turn makes no link and no tag).
Private crumbs stay out unless everything=True. The same store gives the same bytes.
"""

import json
import os
import posixpath
import re
import time
import unicodedata

from .crumbs import Crumb, get_crumb, put_crumb
from .history import commit
from .ingest import leaves, memory_row
from .lines import get_lines, put_text, split_lines
from .view import cue_edges

MANIFEST = ".breadcrumbs-vault"  # the files of the last export, one per line (Obsidian shows no dot file)
MAX_NAME = 200  # bytes: room for " (crumb id).md" under the 255-byte limit of the common file systems
# A file name cannot hold \ / : * ? " < > | or a control character (Windows, macOS, Linux). An Obsidian
# link cannot hold # | ^ : %% [[ ]] (help.obsidian.md/links, "Invalid characters").
_BAD = re.compile(r'(?:[\\/:*?"<>|#^\[\]\x00-\x1f\x7f]|%{2,})+')
_SPACE = re.compile(r"\s+")
_ROLE = re.compile(r"^(?:user|assistant): ")
_RESERVED = re.compile(r"(?i)^(?:con|prn|aux|nul|com\d|lpt\d)(?:\..*)?$")  # Windows device names
_TICKS = re.compile(rb"`+")
# Obsidian reads no link inside code (a fenced block or an inline span) or a %% comment %%.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE = re.compile(r"(\x60+)[^\n]*?(?<!\x60)\1(?!\x60)")  # \x60: a backtick (a bang, a backtick and text is a shell snippet to Hermes's scanner)
_COMMENT = re.compile(r"%%.*?%%", re.S)
_LINK = re.compile(r"!?\[\[([^\[\]\n]+?)\]\]")


def safe_name(text):
    """A note name that each file system and an Obsidian [[link]] can hold: NFC, the bad characters
    replaced by spaces, no dot or space at the ends, at most MAX_NAME bytes; "untitled" when empty."""
    name = _SPACE.sub(" ", _BAD.sub(" ", unicodedata.normalize("NFC", text))).strip(" .")
    while len(name.encode("utf-8")) > MAX_NAME:
        name = name[:-1].rstrip(" .")
    return name or "untitled"


def unique_names(items):
    """{key: name} for items [(key, name)]. A name that two keys share (the case does not count: macOS and
    Windows file systems ignore it) or a reserved name gets " (key)"."""
    count = {}
    for _key, name in items:
        count[name.casefold()] = count.get(name.casefold(), 0) + 1
    return {key: name if count[name.casefold()] == 1 and not _RESERVED.match(name) else "%s (%s)" % (name, key)
            for key, name in items}


def _prose(text):
    """text without its fenced code blocks, inline code spans and %% comments %%."""
    keep, fence = [], None
    for line in text.splitlines(True):
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            else:
                keep.append(line)
        elif m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and line.strip() == m.group(1):
            fence = None
    return _COMMENT.sub(" ", _CODE.sub(" ", "".join(keep)))


def wikilinks(text):
    """The targets of the [[links]] and ![[embeds]] of a note, in order: the note part of
    [[note#heading|shown text]] (in a table, \\| counts as |). A link to a heading of the same note is left out."""
    out = []
    for m in _LINK.finditer(_prose(text)):
        target = m.group(1).replace("\\|", "|").partition("|")[0].partition("#")[0].strip()
        if target:
            out.append(target)
    return out


# -- export --------------------------------------------------------------------------------------------
def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(ts)))  # an Obsidian date & time property


def _q(text):
    return json.dumps(text, ensure_ascii=False)  # a JSON string is a YAML double-quoted string


def _bytes(store, crumb):
    data = b"".join(b"".join(get_lines(store, doc, a, b)) for doc, a, b in crumb.ranges)
    try:
        if b"\x00" not in data:
            data.decode("utf-8")
            return data
    except UnicodeDecodeError:
        pass
    return b"(binary, %d bytes: not shown)\n" % len(data)


def _cue_text(v, cid):
    """The text that the cue words of a session come from (Zoom._cue_text), from public crumbs only."""
    c = get_crumb(v.store, cid)
    if not c.public or memory_row(v.store, c):
        return ""
    return "".join(v.zoom.source("%s:%d-%d" % (doc, a, b)) for doc, a, b in c.ranges) + \
        "".join(_cue_text(v, k) for k in c.kids)


def _sessions(v, everything):
    """The sessions to export, newest first: one per session crumb (on a "sess-" ref)."""
    out, seen = [], set()
    for ref in v.refs.names():
        cid = v.refs.get(ref) if ref.startswith("sess-") else None
        if not cid or cid in seen:
            continue
        seen.add(cid)
        c = get_crumb(v.store, cid)
        if not (everything or c.public):
            continue
        turns = []
        for tid in leaves(v.store, cid):
            tc = get_crumb(v.store, tid)
            if tid != cid and (everything or tc.public):
                turns.append((tid, tc, _bytes(v.store, tc)))
        cues = v.zoom.cue_words(cid).split() if everything else v.index.signature(_cue_text(v, cid))
        summary = v.index.summary_of(cid)
        sc = get_crumb(v.store, summary) if summary else None
        out.append({"id": cid, "crumb": c, "title": safe_name(_ROLE.sub("", c.title)), "turns": turns,
                    "cues": cues, "ts": max([int(tc.meta.get("ts") or 0) for _t, tc, _d in turns] + [0]),
                    "summary": (summary, sc, _bytes(v.store, sc)) if sc and (everything or sc.public) else None,
                    "links": []})
    out.sort(key=lambda s: (-s["ts"], s["id"]))
    return out


def _when(s):
    return " \u00b7 " + _iso(s["ts"]) if s["ts"] else ""


def _link(s):
    return "[[%s|%s]]" % (s["path"], s["name"])


def _summary(s):
    """The summary as an Obsidian callout: marked as model text, with the ids that it comes from."""
    sid, sc, data = s["summary"]
    m = sc.meta
    out = ["> [!summary] Written by a model (%s), not by a person" % (m.get("model") or m.get("by") or "unknown")]
    out += ["> " + line if line.strip() else ">" for line in data.decode("utf-8", "replace").splitlines()]
    ids = ["summary `%s`" % sid, "source " + ", ".join("`%s`" % h for h in sc.heritage)]
    ids += ["model input `%s`" % m["input"]] if m.get("input") else []
    ids += ["model sha256 `%s`" % m["model_sha256"]] if m.get("model_sha256") else []
    return out + [">", "> " + " \u00b7 ".join(ids), ""]


def _session_note(s):
    c = s["crumb"]
    head = ["---", "crumb: " + _q(s["id"])]
    if c.meta.get("sess"):
        head.append("session: " + _q(str(c.meta["sess"])))
    if s["ts"]:
        head.append("time: " + _iso(s["ts"]))
    head.append("turns: %d" % len(s["turns"]))
    if s["summary"]:
        head.append("summary: " + _q(s["summary"][0]))
    head.append("tags:" + ("" if s["cues"] else " []"))
    head += ["  - " + _q(w) for w in s["cues"]]
    head += ["---", "# " + s["name"], ""]
    if s["summary"]:
        head += _summary(s)
    if s["links"]:
        head += ["## Related", ""] + ["- %s \u00b7 shared: %s" % (_link(o), ", ".join(shared))
                                      for o, shared in s["links"]] + [""]
    head.append("## Turns")
    out = ["\n".join(head).encode("utf-8") + b"\n"]
    for tid, tc, data in s["turns"]:
        role = _SPACE.sub(" ", str(tc.meta.get("role") or tc.kind))
        when = " \u00b7 " + _iso(tc.meta["ts"]) if tc.meta.get("ts") else ""
        fence = b"`" * (max([2] + [len(t) for t in _TICKS.findall(data)]) + 1)
        out.append(("\n### %s \u00b7 `%s`%s\n" % (role, tid, when)).encode("utf-8"))
        out.append(fence + b"text\n" + data + (b"\n" if data and not data.endswith(b"\n") else b"") + fence + b"\n")
    return b"".join(out)


def _write(out, files):
    """Write files {vault path: bytes} into folder out and remove the files of the last export that are
    gone. A file with the same bytes is not written again. Returns (written, removed)."""
    os.makedirs(out, exist_ok=True)
    manifest = os.path.join(out, MANIFEST)
    try:
        with open(manifest, encoding="utf-8") as f:
            old = [line for line in f.read().splitlines() if line]
    except FileNotFoundError:
        if any(not n.startswith(".") for n in os.listdir(out)):
            raise FileExistsError("%s holds files that no vault export wrote" % out) from None
        old = []
    written = removed = 0
    for rel in sorted(files):
        path = os.path.join(out, *rel.split("/"))
        try:
            with open(path, "rb") as f:
                if f.read() == files[rel]:
                    continue
        except OSError:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(files[rel])
        os.replace(path + ".tmp", path)
        written += 1
    keep = {rel.casefold() for rel in files}
    for rel in old:
        parts = rel.split("/")
        if rel.casefold() in keep or not rel.endswith(".md") or any(p in ("", ".", "..") for p in parts):
            continue
        path = os.path.join(out, *parts)
        if os.path.isfile(path):
            os.remove(path)
            removed += 1
    with open(manifest + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(rel + "\n" for rel in sorted(files)))
    os.replace(manifest + ".tmp", manifest)
    return written, removed


def _hub_note(word, name, sessions):
    out = ["---", "cue: " + _q(word), "sessions: %d" % len(sessions), "---", "# " + name, "",
           "The sessions with the cue word %s, newest first:" % _q(word), ""]
    out += ["- %s%s" % (_link(s), _when(s)) for s in sessions]
    return ("\n".join(out) + "\n").encode("utf-8")


def _index_note(sessions, hubs):
    out = ["---", "sessions: %d" % len(sessions), "cues: %d" % len(hubs), "---", "# breadcrumbs", "",
           "A breadcrumbs store as notes. Each session is a note in sessions/. Each cue word is a note in cues/:"
           " it links to the sessions with that word. Sessions that share cue words link to each other.", "",
           "## Sessions (newest first)", ""]
    out += ["- %s%s \u00b7 %d turns" % (_link(s), _when(s), len(s["turns"])) for s in sessions]
    out += ["", "## Cue words", ""]
    out += ["- [[cues/%s|%s]] \u00b7 %d session%s" % (name, name, n, "" if n == 1 else "s") for name, n in hubs]
    return ("\n".join(out) + "\n").encode("utf-8")


def export_vault(v, out, everything=False):
    """Write the vault of View v into folder out. Returns counts. A folder that holds other files and no
    MANIFEST raises FileExistsError: the export writes only into a folder that it wrote before.

    sessions/NAME.md: one note per session; it links to each session that shares cue words with it (the
      edges of View.graph: view.cue_edges over the sessions, newest first).
    cues/WORD.md: one hub note per cue word; it links to each session note with that word, newest first.
    index.md: links to each session note (newest first) and to each hub note (in word order)."""
    t0 = time.perf_counter()
    sessions = _sessions(v, everything)
    names = unique_names([(s["id"], s["title"]) for s in sessions])
    by_id = {}
    for s in sessions:
        s["name"] = names[s["id"]]
        s["path"] = "sessions/" + s["name"]
        by_id[s["id"]] = s
    for e in cue_edges([{"id": s["id"], "cues": s["cues"]} for s in sessions]):
        a, b = by_id[e["source"]], by_id[e["target"]]
        a["links"].append((b, e["shared"]))
        b["links"].append((a, e["shared"]))
    hubs = {}
    for s in sessions:
        for w in dict.fromkeys(w.lower() for w in s["cues"]):
            hubs.setdefault(w, []).append(s)
    words = sorted(hubs)
    hub_names = unique_names([(w, safe_name(w)) for w in words])
    files = {s["path"] + ".md": _session_note(s) for s in sessions}
    for w in words:
        files["cues/%s.md" % hub_names[w]] = _hub_note(w, hub_names[w], hubs[w])
    files["index.md"] = _index_note(sessions, [(hub_names[w], len(hubs[w])) for w in words])
    written, removed = _write(out, files)
    return {"sessions": len(sessions), "cues": len(words), "notes": len(files),
            "bytes": sum(len(b) for b in files.values()), "written": written, "removed": removed,
            "seconds": round(time.perf_counter() - t0, 3)}


# -- import --------------------------------------------------------------------------------------------
def _note_paths(root):
    """The vault paths of the notes under folder root, sorted: each .md file outside the dot folders
    (Obsidian keeps its settings in .obsidian and its deleted notes in .trash)."""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.lower().endswith(".md") and not name.startswith("."):
                out.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return sorted(out)


def _key(text):
    return unicodedata.normalize("NFC", text).casefold()  # Obsidian links ignore the case


def _resolve(target, src, by_path, by_name):
    """The vault path of the note that [[target]] in note src names, or None. As Obsidian does: the full
    path, a path from the folder of src, or the end of a path (a note name). When more notes match, the
    note in the folder of src wins, then the shortest path, then the first in sorted order."""
    t = unicodedata.normalize("NFC", target)
    if t.lower().endswith(".md"):
        t = t[:-3]
    folder = posixpath.dirname(src)
    if t.startswith(("./", "../")):
        t = posixpath.normpath(posixpath.join(folder, t))
    t = t.lstrip("/")
    key = t.casefold()
    for k in (key, _key(posixpath.join(folder, t)) if folder else key):
        if k in by_path:
            return by_path[k]
    found = [r for r in by_name.get(key.rpartition("/")[2], ()) if _key(r[:-3]).endswith("/" + key)]
    return min(found, key=lambda r: (posixpath.dirname(r) != folder, r.count("/"), len(r), r)) if found else None


def import_vault(store, index, refs, root, ts=None):
    """Read the Obsidian vault in folder root into the store. Returns counts.

    Each note (.md file) becomes a "note" crumb with its exact bytes (opened at level 1, it gives the file
    bytes) and its vault path in meta. Each [[link]] to another note becomes a link between the two
    crumbs: in the "links" of the "vault" crumb over the notes, and in the index (so recall finds the
    linked note too). The vault crumb goes on ref vault-NAME and on main. The crumbs are content-addressed:
    an unchanged vault adds nothing, and a changed note adds a new crumb and keeps the old one."""
    t0 = time.perf_counter()
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    name = os.path.basename(os.path.abspath(root).rstrip(os.sep)) or "vault"
    paths = _note_paths(root)
    crumbs, texts, added, size = {}, {}, 0, 0
    for rel in paths:
        with open(os.path.join(root, *rel.split("/")), "rb") as f:
            data = f.read()
        size += len(data)
        doc, n = put_text(store, data), len(split_lines(data))
        before = len(store)
        cid = put_crumb(store, Crumb("note", posixpath.basename(rel)[:-3], ranges=[[doc, 0, n]],
                                     meta={"src": "obsidian", "path": rel}))
        added += len(store) > before
        texts[rel] = data.decode("utf-8", "replace")
        if n:
            index.add(cid, "%s:0-%d" % (doc, n), texts[rel], kind="note")
        crumbs[rel] = cid
    by_path, by_name = {}, {}
    for rel in paths:
        by_path.setdefault(_key(rel[:-3]), rel)
        by_name.setdefault(_key(posixpath.basename(rel)[:-3]), []).append(rel)
    links, unresolved = set(), set()
    for rel in paths:
        for target in wikilinks(texts[rel]):
            dest = _resolve(target, rel, by_path, by_name)
            if dest is None:
                unresolved.add((rel, target))
            elif dest != rel:
                links.add((crumbs[rel], crumbs[dest]))
    for a, b in sorted(links):
        index.link(a, b)
    rid = put_crumb(store, Crumb("vault", name, kids=[crumbs[rel] for rel in paths],
                                 meta={"src": "obsidian", "links": [[a, b] for a, b in sorted(links)]}))
    ref = "vault-" + re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:100]
    if refs.get(ref) != rid:
        refs.set(ref, rid)
        commit(store, refs, "main", [rid], "vault import " + name, ts=ts)
    store.flush()
    index.commit()
    return {"notes": len(paths), "added": added, "links": len(links), "unresolved": len(unresolved),
            "bytes": size, "root": rid, "ref": ref, "seconds": round(time.perf_counter() - t0, 3)}
