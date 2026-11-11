"""breadcrumbs SpaceGraph: the read-only backend of the graph, Gantt and Venn views.

The Hermes dashboard mounts this router at /api/plugins/breadcrumbs/ (see dashboard/manifest.json).
The dashboard's session secret guards each route. The user must put "breadcrumbs" in plugins.enabled.

Each route reads one breadcrumbs store through breadcrumbs.view.View, from the package in this same
plugin folder, so this file holds no store code. The routes are the API table of
`python -m breadcrumbs serve` (breadcrumbs.view.API) without the token k: the same paths without
"/api", the same parameters, the same status codes (404 for an unknown id, 400 for a bad value) and
the same error body {"error": ...}. So the desktop views can read either server.

    GET /graph?root=          the sessions (no root), or one crumb and its parts
    GET /session?sid=         one session: nodes and edges (kid, heritage, nbr, call)
    GET /context?sid=         the Gantt: events, turns, rows of bars
    GET /venn?sid=&at=        the Venn at one event: sets and regions, with bytes
    GET /crumb?id=&level=     the text of one crumb at one zoom level (a card)
    GET /find?q=&n=   /path?id=&within=   /trail
    GET /stores               this plugin: the breadcrumbs stores on this machine
    GET /health               this plugin: the package folder

Each route also takes store=<profile name> ("default" = the store of ~/.hermes). Without it, a route
reads the store of the profile that runs the dashboard. The name is "store", not "profile": the
dashboard uses ?profile= for its own profile scope. A store outside the profiles (for example, the two
arms of an A/B run) gets a name in stores.local.json in the plugin folder: {"name": "/abs/folder"}.
That file is local (gitignored). A name there must not be "default" or the name of a profile.

The no-vis tags (breadcrumbs/novis.py) hold for every route, the log views too: /stores leaves out a
profile that opted out of visualization, and its routes answer 403; a session that opted out is left
out of every answer, and a request for it by id is 404.

A reader must never block or change the provider's store. View (breadcrumbs bffe8d4 and later)
opens the store, the index and the refs read-only: a folder without a store raises FileNotFoundError
and gets no file. This file keeps one View and one lock for each store. A new View replaces the old
one when index.db or blobs.pack is replaced (for example, by a new backfill).

The log views read Hermes's own session log (state.db) through breadcrumbs/logview.py, with no
breadcrumbs store, so they draw any session, also with no breadcrumbs store:

    GET /log/sessions?limit=  the sessions, newest first (id, title, started, messages, blocks)
    GET /log/session?sid=     one session as a graph: its messages, nbr and call edges
    GET /log/context?sid=     the Gantt of the session's context, in the shape of /context

For these routes, store=<name> names a Hermes home and its state.db: empty = the active profile,
"default" = the Hermes root, a profile name, or a name in stores.local.json whose folder (or the
parent of that folder) holds a state.db. Each read opens state.db read-only (mode=ro, query_only)
and closes it at once. The status codes and the error body are those of the routes above.
"""

import importlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "_breadcrumbs_dashboard"  # a private name: never the provider's own "breadcrumbs" (the dev shim purges it)
STORE_FILES = ("index.db", "refs", "store/blobs.pack", "store/blobs.idx")  # for /stores only
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
OWN_PARAMS = ("store",)  # parameters of this plugin, not of breadcrumbs' API
EXTRA_FILE = PLUGIN_ROOT / "stores.local.json"  # named stores outside the profiles (read at each request)

router = APIRouter()
clock = time.time  # the clock of the heat values; the contract tests set a fixed one

_views = {}  # resolved store root -> [View, lock, identity of index.db and blobs.pack]
_views_lock = threading.Lock()
_load_lock = threading.Lock()


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def active_home() -> Path:
    """The Hermes home of the profile that runs this router."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def hermes_root() -> Path:
    """The folder that holds profiles/ (normally ~/.hermes)."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except ImportError:
        return Path.home() / ".hermes"


def package_dir() -> Path:
    """The breadcrumbs package of this plugin folder."""
    return PLUGIN_ROOT / "breadcrumbs"


def view_module():
    """breadcrumbs.view, imported under the private name PACKAGE (relative imports only)."""
    with _load_lock:
        mod = sys.modules.get(PACKAGE + ".view")
        if mod is not None:
            return mod
        where = package_dir()
        spec = importlib.util.spec_from_file_location(PACKAGE, where / "__init__.py",
                                                      submodule_search_locations=[str(where)])
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load the breadcrumbs package at %s" % where)
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE] = pkg
        try:
            spec.loader.exec_module(pkg)
            return importlib.import_module(PACKAGE + ".view")
        except Exception:
            for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
                del sys.modules[name]
            raise


def novis_module():
    """breadcrumbs/novis.py (the no-vis tags), from the same private package as the View."""
    view_module()
    return importlib.import_module(PACKAGE + ".novis")


def _hide(home: Path):
    """The novis.Hide of the store of Hermes home `home`: the sessions that opted out of visualization. A profile that
    opted out (or a tags file that cannot be read) is refused with 403."""
    hide = novis_module().Hide(home, hermes=str(hermes_root()))
    why = hide.why()
    if why:
        raise Refused(403, why)
    return hide


def extra_stores() -> dict:
    """The named stores in EXTRA_FILE: {name: folder}. A name that is not a valid name, is "default", or
    names a profile is left out, and so is a folder that is not an absolute path."""
    try:
        data = json.loads(Path(EXTRA_FILE).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise Refused(500, "%s: %s" % (Path(EXTRA_FILE).name, e))
    if not isinstance(data, dict):
        raise Refused(500, "%s must hold one object: {name: folder}" % Path(EXTRA_FILE).name)
    profiles = hermes_root() / "profiles"
    return {name: Path(folder) for name, folder in data.items()
            if isinstance(name, str) and PROFILE_NAME.fullmatch(name) and name != "default"
            and not (profiles / name).exists() and isinstance(folder, str) and os.path.isabs(folder)}


def store_root(store: str) -> Path:
    """The breadcrumbs folder for store=<name>, or the active profile's when store is empty."""
    if not store:
        return active_home() / "breadcrumbs"
    if not PROFILE_NAME.fullmatch(store):
        raise Refused(400, "store must be a profile name, a name in stores.local.json or 'default', not %r" % store)
    extra = extra_stores().get(store)
    if extra is not None:
        return extra
    if store == "default":
        return hermes_root() / "breadcrumbs"
    return hermes_root() / "profiles" / store / "breadcrumbs"


def is_store(root: Path) -> bool:
    return all((root / f).exists() for f in STORE_FILES)


def _identity(root: Path):
    out = []
    for f in ("index.db", "store/blobs.pack"):
        st = os.stat(root / f)
        out.append((st.st_dev, st.st_ino))
    return tuple(out)


def _view(store: str):
    root = store_root(store)
    hide = _hide(root.parent)
    try:
        ident = _identity(root)
        key = str(root.resolve())
        with _views_lock:
            entry = _views.get(key)
            if entry is None or entry[2] != ident:
                if entry is not None:
                    entry[0].close()
                entry = [view_module().View(key, clock=lambda: clock(), hide=hide), threading.Lock(), ident]
                _views[key] = entry
    except FileNotFoundError:
        raise Refused(404, "no breadcrumbs store at %s" % root)
    return entry[0], entry[1]


WARM_ENV = "SPACEGRAPH_NO_WARM"  # set: no warm-up thread when the router loads (the tests set it)


def warm(store: str = "") -> bool:
    """Open the View of a store and run graph() once, so the first request of a pane does not pay the cold
    cost: breadcrumbs builds its caches on the first call (b65fc11: about 9 s for the 64-session top-level
    store, then 0.01 s). The View stays open, as every View here does. True when it is open and warm."""
    try:
        view, lock = _view(store)
        with lock:
            view.graph()
        return True
    except Exception:  # no store, or no unpacked package: the routes report that to the pane
        return False


def _active_store_name() -> str:
    """The /stores name of the active profile's store: "default" for the Hermes root, else the profile name.
    Resolved on the loading thread: a new thread does not get Hermes's context-local profile."""
    home = active_home().resolve()
    root = hermes_root().resolve()
    return "default" if home == root else home.name if home.parent == root / "profiles" else ""


def _warm_at_load():
    if os.environ.get(WARM_ENV):
        return None
    thread = threading.Thread(target=warm, args=(_active_store_name(),), name="breadcrumbs-warm", daemon=True)
    thread.start()
    return thread


def close_all():
    """Close every open View (the dashboard's shutdown, and the tests). View.close() is idempotent."""
    with _views_lock:
        for view, lock, _ in _views.values():
            with lock:
                view.close()
        _views.clear()


def _error(status, message):
    return JSONResponse({"error": message}, status_code=status)


@router.get("/health")
def health():
    try:
        view_module()
        error = None
    except Exception as e:  # the package of this plugin folder does not import
        error = "%s: %s" % (type(e).__name__, e)
    return {"package": str(package_dir()), "error": error, "home": str(active_home())}


@router.get("/stores")
def stores():
    """Each folder that holds a breadcrumbs store, with the number of its sessions."""
    root = hermes_root()
    found = [("default", root / "breadcrumbs", False)]
    profiles = root / "profiles"
    if profiles.is_dir():
        found += sorted((p.name, p / "breadcrumbs", False) for p in profiles.iterdir()
                        if p.is_dir() and PROFILE_NAME.fullmatch(p.name))
    try:
        found += sorted((name, folder, True) for name, folder in extra_stores().items())
    except Refused as e:
        return _error(e.status, str(e))
    active = (active_home() / "breadcrumbs").resolve()
    out = []
    for name, path, extra in found:
        if is_store(path):
            try:
                _hide(path.parent)
            except Refused:  # opted out of visualization (novis): not listed
                continue
            sessions = sum(1 for n in os.listdir(path / "refs") if n.startswith("sess-") and not n.endswith(".tmp"))
            out.append({"store": name, "root": str(path), "sessions": sessions, "active": path.resolve() == active,
                        "extra": extra})
    return {"stores": out}


# -- the log views: Hermes's own state.db, no breadcrumbs -------------------------------------------

LOG_DB = "state.db"


def logview_module():
    """breadcrumbs/logview.py (standard library only), from the same private package as the View."""
    view_module()
    return importlib.import_module(PACKAGE + ".logview")


def log_db(store: str) -> Path:
    """The state.db for store=<name>: the active profile's (empty), the root's ("default"), a
    profile's, or the one in (or beside) a folder that stores.local.json names."""
    if not store:
        return active_home() / LOG_DB
    if not PROFILE_NAME.fullmatch(store):
        raise Refused(400, "store must be a profile name, a name in stores.local.json or 'default', not %r" % store)
    extra = extra_stores().get(store)
    if extra is not None:
        for folder in (extra, extra.parent):
            if (folder / LOG_DB).is_file():
                return folder / LOG_DB
        raise Refused(404, "no %s in %s or %s" % (LOG_DB, extra, extra.parent))
    if store == "default":
        return hermes_root() / LOG_DB
    return hermes_root() / "profiles" / store / LOG_DB


def _log_route(request: Request, call, sid=None):
    """call(logview, state.db path, connect) with the status codes of the routes above. The no-vis tags hold: a profile
    that opted out of visualization is refused (403); a session that did is unknown (404, sid) and left out of
    a list."""
    path = None
    try:
        path = log_db(request.query_params.get("store", ""))
        hide = _hide(path.parent)
        if sid is not None and hide(sid):
            raise KeyError(sid)
        lv = logview_module()
        out = call(lv, path, lv.hermes_connect())
        if "sessions" in out:
            out["sessions"] = [s for s in out["sessions"] if not hide(s["id"])]
        for row in out.get("rows", []):  # a message that another chat opened from a session that opted out: no label
            if row.get("session") and hide(row["session"]):
                row["label"] = ""
        return out
    except Refused as e:
        return _error(e.status, str(e))
    except FileNotFoundError:
        return _error(404, "no %s at %s" % (LOG_DB, path))
    except KeyError as e:
        return _error(404, "unknown session: %s" % e)
    except ValueError as e:
        return _error(400, str(e))
    except sqlite3.Error as e:
        return _error(503, "%s: %s" % (LOG_DB, e))


@router.get("/log/sessions")
def log_sessions(request: Request):
    """The sessions of the log, newest first."""
    limit = request.query_params.get("limit", "50")
    return _log_route(request, lambda lv, path, connect: lv.sessions(path, limit=limit, connect=connect))


@router.get("/log/session")
def log_session(request: Request):
    """One session of the log as nodes and edges."""
    sid = request.query_params.get("sid", "")
    return _log_route(request, lambda lv, path, connect: lv.session(path, sid, connect=connect), sid)


@router.get("/log/context")
def log_context(request: Request):
    """The Gantt of one session's context, from the log alone."""
    sid = request.query_params.get("sid", "")
    return _log_route(request, lambda lv, path, connect: lv.context(path, sid, connect=connect), sid)


@router.get("/{name}")
def api(name: str, request: Request):
    """One entry of breadcrumbs' API table, with breadcrumbs' own parameters and status codes."""
    try:
        route = view_module().API.get("/api/" + name)
    except RuntimeError as e:
        return _error(503, str(e))
    if route is None:
        return _error(404, "not found")
    q = {k: v for k, v in request.query_params.items() if k not in OWN_PARAMS}
    try:
        view, lock = _view(request.query_params.get("store", ""))
        with lock:
            return route(view, q)
    except Refused as e:
        return _error(e.status, str(e))
    except KeyError as e:
        return _error(404, "unknown id: %s" % e)
    except (ValueError, TypeError) as e:
        return _error(400, str(e))


_warm_thread = _warm_at_load()  # at router load: warm the active profile's store in the background
