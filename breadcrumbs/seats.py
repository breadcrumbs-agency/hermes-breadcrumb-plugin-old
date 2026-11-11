"""Seats: other memory systems in breadcrumbs, each one case by case.

Hermes runs one external memory provider. breadcrumbs takes in the others in one of three ways:
  reuse   run a provider that Hermes has (in its core or installed) THRU breadcrumbs (the slot)
  clone   copy a provider that Hermes took out of its core from Hermes's own git history: its files at the
          last core commit and Hermes's license go into the profile's plugins; CLONED.json names both
          commits and the SHA-256 of each file. A copy that imports plugins.memory.<name> is refused
          (it loads only from the core): install that one from the plugin catalog
  borrow  read the data that the system stores (files, SQLite) without running it

To each system breadcrumbs adds permanence and mobility:
  snap     the exact bytes of its state files: a "snap" crumb under the ref seat-<name>, with a manifest
           (name, SHA-256, size of each file) and one "file" crumb per file; a SQLite file is read through its
           backup API, so the copy has its WAL rows. publish --deep and share carry it to another agent
  harvest  each record that it stores, as a "slot" crumb (meta src = the system, key = its own id) under a
           "seat" crumb; indexed once, so crumb_find finds it after the system is gone
  restore  write the files of a snap back, here or in another home (after a CAR import)
  replay   give kept texts to another system through its own write path; seat-<that system> records
           which crumbs went there
"""

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from urllib.parse import quote

from .crumbs import Crumb, get_crumb, put_crumb
from .history import commit, log
from .ingest import leaves, put_tree
from .lines import get_lines, put_text, split_lines

HERMES_REPO = os.environ.get("BREADCRUMBS_HERMES_REPO") or os.path.expanduser("~/.hermes/hermes-agent")

# The memory systems that Hermes has had in its core. way: the first choice for this system.
# data: its state under HERMES_HOME (snap and harvest read it). write: how replay gives it
# a text: ("hook", target[, targets]) calls on_memory_write("add", target, text), only for the targets that
# the system keeps (honcho keeps "user" only); ("tool", name, arg) calls that tool (no on_memory_write).
CASES = {
    "builtin": {"way": "borrow", "data": ["memories/MEMORY.md", "memories/USER.md"]},
    "holographic": {"way": "clone", "data": ["memory_store.db"], "write": ("hook", "memory")},
    "byterover": {"way": "reuse", "data": ["byterover"], "write": ("hook", "memory")},
    "retaindb": {"way": "reuse", "write": ("hook", "memory")},
    "mem0": {"way": "reuse", "write": ("tool", "mem0_add", "content")},
    "honcho": {"way": "reuse", "write": ("hook", "user", ("user",))},
    "supermemory": {"way": "reuse", "write": ("hook", "memory")},
    "hindsight": {"way": "reuse", "write": ("tool", "hindsight_retain", "content")},
    "openviking": {"way": "reuse", "write": ("hook", "memory")},
}
ENTRY = "\n\u00a7\n"  # the line between two entries of a Hermes memory file
SQLITE = b"SQLite format 3\x00"
SIDE = ("-wal", "-shm", "-journal")  # SQLite side files: the backup API copy holds what they hold


# -- clone --------------------------------------------------------------------------------------------
def _git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True).stdout.decode("utf-8")


def removed(name, repo=HERMES_REPO):
    """(last core commit, removal commit) of a provider that Hermes took out of its core, else None."""
    path = "plugins/memory/%s/__init__.py" % name
    try:
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", "HEAD:" + path], capture_output=True).returncode == 0:
            return None  # still in the core
        gone = _git(repo, "log", "--diff-filter=D", "-1", "--format=%H", "--", path).strip()
        return (_git(repo, "rev-parse", gone + "^").strip(), gone) if gone else None
    except (OSError, subprocess.CalledProcessError):
        return None


def clone(name, plugins_dir, repo=HERMES_REPO):
    """Copy the provider `name` from Hermes's git history into plugins_dir/<name>: its files at the last core
    commit and Hermes's LICENSE. Returns the dir. LookupError when Hermes still has it (reuse it), never had
    it, or its copy imports plugins.memory.<name> (it loads only from the core: install it from the catalog)."""
    found = removed(name, repo)
    if not found:
        raise LookupError("%s: not a provider that Hermes took out of its core (reuse it if Hermes has it)" % name)
    last, gone = found
    dest = os.path.join(plugins_dir, name)
    if os.path.exists(dest):
        raise FileExistsError(dest)
    prefix, blobs = "plugins/memory/%s/" % name, {}
    for path in _git(repo, "ls-tree", "-r", "--name-only", last, "--", prefix).splitlines() + ["LICENSE"]:
        try:
            data = subprocess.run(["git", "-C", repo, "show", "%s:%s" % (last, path)], check=True,
                                  capture_output=True).stdout
        except subprocess.CalledProcessError:
            continue  # a history without a LICENSE file
        blobs[path[len(prefix):] if path.startswith(prefix) else path] = data
    core = sorted(rel for rel, data in blobs.items() if rel.endswith(".py") and
                  ("plugins.memory." + name).encode() in data)
    if core:
        raise LookupError("%s: its last core copy imports plugins.memory.%s (%s), so it loads only from Hermes's "
                          "core; install it from the plugin catalog" % (name, name, ", ".join(core[:3])))
    os.makedirs(plugins_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="." + name, dir=plugins_dir)  # a dot dir: Hermes does not load it
    for rel, data in blobs.items():
        os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
        with open(os.path.join(tmp, rel), "wb") as f:
            f.write(data)
    try:
        origin = _git(repo, "remote", "get-url", "origin").strip()
    except subprocess.CalledProcessError:
        origin = os.path.abspath(repo)
    files = {rel: hashlib.sha256(data).hexdigest() for rel, data in blobs.items()}
    with open(os.path.join(tmp, "CLONED.json"), "w", encoding="utf-8") as f:
        json.dump({"name": name, "repo": origin, "commit": last, "removed_in": gone, "files": files}, f,
                  indent=1, sort_keys=True)
    os.rename(tmp, dest)
    return dest


# -- snap and restore -----------------------------------------------------------------------------------
def _hermes(home):
    """Hermes's provider loader for the profile at home, or None outside Hermes's Python."""
    import importlib.util
    try:
        if importlib.util.find_spec("plugins.memory") is None:
            return None
    except (ImportError, ValueError):
        return None
    os.environ["HERMES_HOME"] = home  # the provider reads this profile's config and secrets
    from plugins.memory import load_memory_provider
    return load_memory_provider


def state_paths(name, home):
    """The state of a system: its CASES data, and inside Hermes its provider's backup_paths()."""
    paths = list(CASES.get(name, {}).get("data", []))
    try:
        load = _hermes(home)
        provider = load(name, register_skills=False) if load and name != "builtin" else None
        paths += [p for p in (provider.backup_paths() or []) if os.path.isabs(p)] if provider else []
    except Exception:  # backup_paths is optional state: the rest is still kept
        pass
    return paths


def _files(home, paths):
    """(name, path) of each file under paths, dirs in name order. A path in home is named relative to home,
    an absolute path (backup_paths) as ~/... under the user's home; others are skipped, as Hermes does."""
    user, out = os.path.expanduser("~"), []
    for p in paths:
        full = p if os.path.isabs(p) else os.path.join(home, p)
        found = [full] if os.path.isfile(full) else sorted(
            os.path.join(b, f) for b, _d, fs in os.walk(full) for f in fs)
        for path in found:
            if os.path.islink(path) or not os.path.isfile(path) or path.endswith(SIDE):
                continue
            rel = os.path.relpath(path, user if os.path.isabs(p) else home).replace(os.sep, "/")
            if not rel.startswith("../"):
                out.append(("~/" + rel if os.path.isabs(p) else rel, path))
    return out


def _read(path):
    """The bytes of a state file. A SQLite database is read through its backup API (read-only), so the copy
    has the rows that are still in its write-ahead log."""
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(SQLITE):
        return data
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)
        try:
            dst = sqlite3.connect(tmp)
            src.backup(dst)
            dst.close()
        finally:
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    except sqlite3.Error:
        return data  # no consistent copy: the bytes as they are
    finally:
        for p in (tmp,) + tuple(tmp + s for s in SIDE):
            if os.path.exists(p):
                os.remove(p)


def snap(store, refs, name, home, paths=None):
    """Keep the exact bytes of a system's state files (CASES data, or paths) as one "snap" crumb, committed
    under the ref seat-<name>. Its range is a manifest, one JSON line [name, sha256, size] per file; its
    kids are "file" crumbs (title = the name, one range = the bytes) in the same order. Both are content,
    so a published snap keeps them in a CAR file (meta does not travel). None when no file is there."""
    files, kids = [], []
    for fname, path in _files(home, CASES.get(name, {}).get("data", []) if paths is None else paths):
        data = _read(path)
        doc = put_text(store, data)
        kids.append(put_crumb(store, Crumb("file", fname, ranges=[[doc, 0, len(split_lines(data))]])))
        files.append([fname, hashlib.sha256(data).hexdigest(), len(data)])
    if not files:
        return None
    manifest = "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in files).encode("utf-8")
    sid = put_crumb(store, Crumb("snap", "%s: %d files" % (name, len(files)),
                                 ranges=[[put_text(store, manifest), 0, len(files)]], kids=kids, meta={"src": name}))
    commit(store, refs, "seat-" + name, [sid], "snap %s %d files" % (name, len(files)))
    store.flush()
    return sid


def _range_bytes(store, rng):
    doc, a, b = rng
    return b"".join(get_lines(store, doc, a, b))


def manifest(store, snap_id):
    """[[name, sha256, size], ...] of a snap, in the order of its kids."""
    crumb = get_crumb(store, snap_id)
    if crumb.kind != "snap" or not crumb.ranges:
        raise ValueError("not a snap crumb: %s" % snap_id)
    return [json.loads(line) for line in _range_bytes(store, crumb.ranges[0]).decode("utf-8").splitlines() if line]


def snap_of(store, refs, name):
    """The newest snap under seat-<name>, or name itself (a crumb id)."""
    for _cid, c in log(store, refs.get("seat-" + name)):
        for root in c.kids:
            if get_crumb(store, root).kind == "snap":
                return root
    return name


def _dest(name, home):
    """Where a snap file name goes: under home, or a ~/ name under the user's home. ValueError for a name
    that leaves it (a snap can come from another agent in a CAR file)."""
    base = os.path.expanduser("~") if name.startswith("~/") else home
    rel = name[2:] if name.startswith("~/") else name
    if not rel or rel.startswith("/") or os.path.isabs(rel):
        raise ValueError("a snap file outside its home: %r" % name)
    base = os.path.abspath(base)
    path = os.path.normpath(os.path.join(base, *rel.split("/")))
    if path == base or os.path.commonpath([path, base]) != base:
        raise ValueError("a snap file outside its home: %r" % name)
    return path


def restore(store, snap_id, home, force=False):
    """Write the files of a snap back under home (a ~/ name under the user's home). Each file is checked
    against its SHA-256 first; a file that is there is replaced only with force. Returns the paths written."""
    try:
        files, kids = manifest(store, snap_id), get_crumb(store, snap_id).kids
    except KeyError:
        raise LookupError("%s: no seat with a snap and no crumb with that id in this store" % snap_id) from None
    if len(files) != len(kids):
        raise ValueError("%s: the manifest names %d files, the snap holds %d" % (snap_id, len(files), len(kids)))
    plan = []
    for (name, digest, _size), kid in zip(files, kids):
        data = _range_bytes(store, get_crumb(store, kid).ranges[0])
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("%s: the kept bytes do not match their SHA-256" % name)
        path = _dest(name, home)
        if os.path.exists(path) and not force:
            raise FileExistsError(path)
        plan.append((path, data))
    for path, data in plan:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if data.startswith(SQLITE):  # an old WAL next to the new database would be read into it
            for side in SIDE:
                if os.path.exists(path + side):
                    os.remove(path + side)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    return [p for p, _ in plan]


# -- harvest ----------------------------------------------------------------------------------------------
def records(name, home):
    """(key, text, meta) of each record that a system keeps in files that breadcrumbs can read (borrow).
    Other systems give none here: their texts come in through the slot."""
    if name == "builtin":
        for target in ("memory", "user"):
            path = os.path.join(home, "memories", target.upper() + ".md")
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    entries = f.read().split(ENTRY)
                for entry in entries:
                    if entry.strip():
                        yield "", entry.strip(), {"target": target}
    elif name == "holographic":
        path = os.path.join(home, "memory_store.db")
        if os.path.isfile(path):
            db = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True)
            try:
                rows = db.execute("SELECT fact_id, content FROM facts ORDER BY fact_id").fetchall()
            finally:
                db.close()
            for fact_id, content in rows:
                yield str(fact_id), content, {}
    elif name == "byterover":
        for fname, path in _files(home, ["byterover"]):
            if fname.endswith(".md"):
                with open(path, encoding="utf-8", errors="replace") as f:
                    yield fname[len("byterover/"):], f.read(), {}


def harvest(store, index, refs, name, home):
    """Keep each record of a system (records()) as a "slot" crumb: meta src = name, key = its own id. The
    same record is the same crumb, indexed once; the new ones are committed under seat-<name> in one "seat"
    crumb. Returns (records read, new crumb ids)."""
    got, new = 0, []
    for key, text, meta in records(name, home):
        got += 1
        data = text.encode("utf-8")
        doc, n = put_text(store, data), len(split_lines(data))
        meta = dict(meta, src=name, **({"key": key} if key else {}))
        cid = put_crumb(store, Crumb("slot", ("%s %s" % (name, key)).strip()[:60], ranges=[[doc, 0, n]], meta=meta))
        if index.add(cid, "%s:0-%d" % (doc, n), text, kind="slot"):
            new.append(cid)
    if new:
        root = put_tree(store, "seat", "%s: %d records" % (name, len(new)), new, meta={"src": name})
        commit(store, refs, "seat-" + name, [root], "harvest %s %d" % (name, len(new)))
    store.flush()
    index.commit()
    return got, new


# -- replay -----------------------------------------------------------------------------------------------
def kept(store, refs, ref):
    """The text crumbs ("slot" and "mem") in the commits of a ref, oldest first, each once."""
    out = []
    for _cid, c in reversed(list(log(store, refs.get(ref)))):
        for root in c.kids:
            out += [x for x in leaves(store, root) if get_crumb(store, x).kind in ("slot", "mem")]
    return list(dict.fromkeys(out))


def _failed(out):
    try:
        return isinstance(out, str) and "error" in json.loads(out)
    except (ValueError, TypeError):
        return False


def load_provider(name, home):
    """The provider called name, started for a replay (inside Hermes's Python only)."""
    load = _hermes(home)
    provider = load(name, register_skills=False) if load else None
    if provider is None or not provider.is_available():
        raise LookupError("%s: no such memory provider here, or it is not set up" % name)
    provider.initialize("breadcrumbs-replay", hermes_home=home, platform="cli", agent_context="primary")
    return provider


def replay(store, refs, ref, provider, name, write=None):
    """Give the kept texts of ref to the provider of another system through its write path (CASES), and
    commit the crumbs that went there under seat-<name>. A text that came from that system or went there
    before is skipped; a failed write is not counted. Returns the crumbs sent."""
    write = write or CASES.get(name, {}).get("write")
    if not write:
        raise ValueError("%s: no known write path; give (\"hook\", target) or (\"tool\", name, arg)" % name)
    there, sent = set(kept(store, refs, "seat-" + name)), []
    for cid in kept(store, refs, ref):
        crumb = get_crumb(store, cid)
        if cid in there or crumb.meta.get("src") == name or not crumb.ranges:
            continue
        if crumb.kind == "mem" and crumb.meta.get("action") == "remove":
            continue  # a mirrored removal keeps the old text: it is not a fact to give
        doc, a, b = crumb.ranges[0]
        text = b"".join(get_lines(store, doc, a, b)).decode("utf-8", "replace")
        try:
            if write[0] == "hook":
                target = crumb.meta.get("target") or (crumb.meta.get("role") if crumb.kind == "mem" else "")
                target = target or write[1]
                if len(write) > 2 and target not in write[2]:
                    continue  # the system drops this target: it is not sent
                provider.on_memory_write("add", target, text)
            elif _failed(provider.handle_tool_call(write[1], {write[2]: text})):
                continue
        except Exception:  # one failed write must not stop the rest; it is not counted as sent
            continue
        sent.append(cid)
    if sent:
        commit(store, refs, "seat-" + name, sent, "replay %s %d" % (ref, len(sent)))
        store.flush()
    return sent


# -- survey -----------------------------------------------------------------------------------------------
def survey(home, repo=HERMES_REPO):
    """One row per memory system: the known ones (CASES) and each provider in home's plugins. where: core
    (Hermes has it), installed or clone (in home's plugins), history (Hermes took it out of its core).
    data: the files of its state that are there."""
    plugins, names = os.path.join(home, "plugins"), list(CASES)
    for d in sorted(os.listdir(plugins)) if os.path.isdir(plugins) else ():
        init = os.path.join(plugins, d, "__init__.py")
        if d in names or d.startswith((".", "_")) or not os.path.isfile(init):
            continue
        with open(init, encoding="utf-8", errors="replace") as f:
            src = f.read(8192)
        if "BreadcrumbsProvider" not in src and ("MemoryProvider" in src or "register_memory_provider" in src):
            names.append(d)  # Hermes's own test (plugins/memory/__init__.py); breadcrumbs is not a seat
    rows = []
    for n in names:
        core = n == "builtin" or os.path.isfile(os.path.join(repo, "plugins", "memory", n, "__init__.py"))
        where = ["core"] if core else []
        if os.path.isfile(os.path.join(plugins, n, "__init__.py")):
            where.append("clone" if os.path.isfile(os.path.join(plugins, n, "CLONED.json")) else "installed")
        if not core and removed(n, repo):
            where.append("history")
        rows.append({"name": n, "way": CASES.get(n, {}).get("way", "reuse"), "where": where,
                     "data": len(_files(home, CASES.get(n, {}).get("data", [])))})
    return rows
