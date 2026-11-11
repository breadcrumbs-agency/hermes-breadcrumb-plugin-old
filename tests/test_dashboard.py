"""The SpaceGraph routes (dashboard/plugin_api.py) over a breadcrumbs store, against the view contract.

tests/fixtures/view-contract.json is the View output for the scenario in tests/test_context.py, with a
fixed clock (test_context.ContractTest writes and checks it). This suite checks:
  1. Each fixture key has a route, and the API table has the routes of the desktop views.
  2. The fields that the desktop views read are in the fixture.
  3. The routes give the fixture, with the status codes of `breadcrumbs serve`. They write nothing, and they
     refuse bad input. They load this folder's breadcrumbs package under a private name.
  4. The Hermes dashboard loader mounts the router.
The route tests need FastAPI (the Python of a Hermes install); without it they skip.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # the scenario never starts the real model
os.environ.setdefault("SPACEGRAPH_NO_WARM", "1")  # loading the router must not warm a real store

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "view-contract.json"
CLOCK = 5000  # the View clock of the fixture
PREFIX = "/api/plugins/breadcrumbs"
# Each fixture key: the View call that made it (test_context.ContractTest) and the route that serves it.
CALLS = {
    "graph": (lambda v: v.graph(), "/graph", {}),
    "session": (lambda v: v.session("s2"), "/session", {"sid": "s2"}),
    "session_s1": (lambda v: v.session("s1"), "/session", {"sid": "s1"}),
    "context": (lambda v: v.context("s2"), "/context", {"sid": "s2"}),
    "context_s3": (lambda v: v.context("s3"), "/context", {"sid": "s3"}),  # one turn under a turn budget
    "venn": (lambda v: v.venn("s2", at=2), "/venn", {"sid": "s2", "at": 2}),
}

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from breadcrumbs import view  # noqa: E402
from test_context import scenario as SCENARIO  # noqa: E402

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: F401
    FASTAPI = None
except Exception as e:  # GraalPy's pydantic, or a Python with no FastAPI
    FASTAPI = "no FastAPI here (%s): run this suite with the Python of a Hermes install" % type(e).__name__


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def roundtrip(obj):
    return json.loads(json.dumps(obj, sort_keys=True))


def snapshot(top):
    """Every file and folder under top: path -> (size, mtime_ns, sha256) for files, "dir" for folders."""
    out = {}
    for base, dirs, files in os.walk(top):
        for d in dirs:
            out[os.path.relpath(os.path.join(base, d), top)] = "dir"
        for f in files:
            p = os.path.join(base, f)
            st = os.stat(p)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, top)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(fh.read()).hexdigest())
    return out


def build_store(hermes):
    """The fixture scenario in the store of profile "demo" under a scratch Hermes root."""
    home = Path(hermes) / "profiles" / "demo"
    home.mkdir(parents=True)
    p, rcpt = SCENARIO(str(home))
    p.shutdown()
    return home, rcpt


def plugin_client(home, hermes, extra_file=None):
    """The plugin router on a bare FastAPI app, pointed at a scratch Hermes root."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    api = load("hermes_dashboard_plugin_breadcrumbs_test", ROOT / "dashboard" / "plugin_api.py")
    setattr(api, "active_home", lambda: home)  # setattr: the test points the module at a scratch root
    setattr(api, "hermes_root", lambda: Path(hermes))
    setattr(api, "clock", lambda: CLOCK)
    setattr(api, "EXTRA_FILE", extra_file or Path(hermes) / "no-stores.local.json")  # never the repo's file
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return api, TestClient(app)


class Table(unittest.TestCase):
    def test_each_fixture_key_has_a_view_call_and_a_route(self):
        self.assertEqual(sorted(json.loads(FIXTURE.read_text())), sorted(CALLS))

    def test_the_api_table_has_the_routes_of_the_desktop_views(self):  # the plugin routes call this table
        names = {"graph", "crumb", "find", "trail", "path", "session", "context", "venn", "ace"}
        self.assertEqual(set(view.API), {"/api/" + n for n in names})


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="bc-dashboard-")
        cls.home, cls.rcpt = build_store(cls.tmp.name)
        cls.root = cls.home / "breadcrumbs"
        cls.fixture = json.loads(FIXTURE.read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class Fields(Contract):
    """The fields that the desktop views read. A subset check: breadcrumbs may add fields."""

    def has(self, items, fields, what):
        self.assertTrue(items, what)
        for item in items:
            self.assertLessEqual(set(fields), set(item), what)

    def test_graph(self):
        g = self.fixture["graph"]
        self.has(g["nodes"], ["id", "kind", "title", "session", "ts", "tokens", "pri", "heat", "cues"], "graph node")
        self.has(g["edges"], ["kind", "source", "target", "w"], "graph edge")
        self.has([e for e in g["edges"] if e["kind"] == "cue"], ["shared"], "cue edge")

    def test_sessions(self):
        for key in ("session", "session_s1"):
            s = self.fixture[key]
            self.has(s["nodes"], ["id", "kind", "title", "tokens", "meta", "ranges"], key + " node")
            self.has(s["edges"], ["kind", "source", "target", "w"], key + " edge")
            self.assertFalse(any("heat" in n for n in s["nodes"]), key)
        self.has([n for n in self.fixture["session_s1"]["nodes"] if "use" in n], [], "a node with use")
        for n in self.fixture["session_s1"]["nodes"]:
            if "use" in n:
                self.assertEqual(set(n["use"]), {"accessed", "uses"})
        self.assertIn("call", {e["kind"] for e in self.fixture["session"]["edges"]})

    def test_context(self):
        for key in ("context", "context_s3"):
            c = self.fixture[key]
            self.has(c["events"], ["id", "ev", "turn", "ts", "bytes", "budget", "ovf", "rate", "band", "q",
                                   "turn_budget", "used", "cap", "want"], key + " event")
            for i, e in enumerate(c["events"]):
                lo, hi = e["band"]
                self.assertLess(lo, hi)
                self.assertLessEqual(len(e["q"]), 120)
                self.assertEqual(bool(e["q"]), e["ev"] in ("recall", "find"), e)  # the Gantt labels these two
                self.assertEqual(e["cap"], e["want"] > e["bytes"], e)  # the ghost bar is want - bytes
                self.assertGreaterEqual(e["want"], e["bytes"], e)
                so_far = sum(x["bytes"] for x in c["events"][:i + 1]
                             if x["turn"] == e["turn"] and x["ev"] in ("recall", "find", "open"))
                self.assertEqual(e["used"], so_far, e)  # the step line of the Gantt
            self.has(c["turns"], ["turn", "crumb", "ts"], key + " turn")
            self.has(c["rows"], ["id", "kind", "label", "session", "bars"], key + " row")
            self.has([b for r in c["rows"] for b in r["bars"]], ["from", "to", "t0", "t1", "level", "bytes", "ev"],
                     key + " bar")
        self.assertEqual({r["session"] for r in self.fixture["context"]["rows"]}, {"s1", "s2"})  # rows by session
        self.assertEqual({e["turn_budget"] for e in self.fixture["context"]["events"]}, {None})  # budget off
        s3 = self.fixture["context_s3"]["events"]
        self.assertEqual({e["turn_budget"] for e in s3}, {756})
        self.assertTrue(any(e["cap"] for e in s3))  # real data for the ghost bar

    def test_venn(self):
        vn = self.fixture["venn"]
        self.assertEqual(set(vn["sets"]), {"recalled", "found", "opened"})
        self.has(list(vn["sets"].values()), ["ids", "bytes"], "set")
        self.has(vn["regions"], ["in", "ids", "bytes", "by"], "region")


@unittest.skipIf(FASTAPI, FASTAPI)
class Routes(Contract):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.before = snapshot(cls.tmp.name)
        cls.api, cls.client = plugin_client(cls.home, cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.api.close_all()
        super().tearDownClass()

    def get(self, route, **params):
        return self.client.get(PREFIX + route, params=params)

    def ok(self, route, **params):
        r = self.get(route, **params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def status(self, route, **params):
        return self.get(route, **params).status_code

    def test_the_routes_give_the_fixture(self):
        for key, (_, route, params) in CALLS.items():
            self.assertEqual(self.ok(route, **params), self.fixture[key], key)

    def test_store_names_the_profile(self):
        self.assertEqual(self.ok("/context", sid="s2", store="demo"), self.fixture["context"])
        listed = self.ok("/stores")["stores"]
        self.assertEqual([(s["store"], s["sessions"], s["active"]) for s in listed], [("demo", 4, True)])

    def test_the_package_is_this_folder_under_a_private_name(self):
        mod = self.api.view_module()
        self.assertEqual(mod.__name__, "_breadcrumbs_dashboard.view")  # never the provider's own "breadcrumbs"
        self.assertIsNot(mod, view)
        self.assertEqual(Path(mod.__file__).resolve(), (ROOT / "breadcrumbs" / "view.py").resolve())
        self.assertEqual(self.api.logview_module().__name__, "_breadcrumbs_dashboard.logview")
        health = self.ok("/health")
        self.assertEqual((health["error"], Path(health["package"])), (None, ROOT / "breadcrumbs"))

    def test_the_other_routes_answer(self):
        rows = self.fixture["context"]["rows"]
        opened = [r for r in rows if any(b["ev"] == "open" for b in r["bars"])][0]["id"]
        self.assertTrue(self.ok("/crumb", id=opened, level=0.5))
        self.assertEqual(self.ok("/crumb", id=opened, level=2), self.ok("/crumb", id=opened, level=1))  # serve clamps
        self.assertTrue(self.ok("/crumb", id=self.fixture["context"]["events"][1]["id"], level=1))  # a receipt
        self.ok("/find", q="bike chain")
        self.ok("/path", id=opened)
        self.ok("/trail")
        self.ok("/graph", root=opened)

    def test_bad_input_gets_the_status_codes_of_serve(self):
        for route in ("/session", "/context", "/venn"):
            self.assertEqual(self.status(route, sid="no-such-session"), 404, route)
        self.assertEqual(self.status("/session"), 404)  # no sid: serve's KeyError
        for at in (99, 5, -1, "x"):
            self.assertEqual(self.status("/venn", sid="s2", at=at), 400, at)
        self.assertEqual(self.status("/no-such-route"), 404)
        self.assertEqual(self.get("/venn", sid="s2", at=99).json().keys(), {"error"})
        for bad in ("../demo", "demo/../../x", ".hidden", "a b"):
            self.assertEqual(self.status("/context", sid="s2", store=bad), 400, bad)

    def test_a_folder_without_a_store_is_404_and_stays_empty(self):
        for name in ("default", "nobody"):
            self.assertEqual(self.status("/context", sid="s2", store=name), 404, name)
        self.assertFalse((Path(self.tmp.name) / "breadcrumbs").exists())
        self.assertFalse((Path(self.tmp.name) / "profiles" / "nobody").exists())

    def test_zz_the_routes_wrote_nothing(self):  # zz: runs after the other route tests
        self.test_the_routes_give_the_fixture()
        self.api.close_all()
        self.api.close_all()  # a second close does nothing
        self.test_the_routes_give_the_fixture()  # the next request opens a new View
        self.test_the_other_routes_answer()
        self.assertEqual(snapshot(self.tmp.name), self.before)


@unittest.skipIf(FASTAPI, FASTAPI)
class Rebuild(Contract):
    def test_a_replaced_index_gets_a_new_view(self):
        api, client = plugin_client(self.home, self.tmp.name)
        try:
            first = client.get(PREFIX + "/context", params={"sid": "s2"}).json()
            old = api._views[str(self.root.resolve())][0]
            copy = self.root / "index.db.new"
            shutil.copy2(self.root / "index.db", copy)
            os.replace(copy, self.root / "index.db")  # a new file (new inode) at the same path
            again = client.get(PREFIX + "/context", params={"sid": "s2"}).json()
            self.assertIsNot(api._views[str(self.root.resolve())][0], old)
            self.assertEqual((first, again), (self.fixture["context"], self.fixture["context"]))
        finally:
            api.close_all()


@unittest.skipIf(FASTAPI, FASTAPI)
class Warm(Contract):
    def test_warm_fills_the_view_that_the_routes_use(self):
        api, client = plugin_client(self.home, self.tmp.name)
        try:
            self.assertIsNone(api._warm_thread)  # SPACEGRAPH_NO_WARM: no thread at load
            self.assertEqual(api._views, {})
            self.assertEqual(api._active_store_name(), "demo")
            self.assertTrue(api.warm("demo"))
            self.assertEqual(list(api._views), [str((self.home / "breadcrumbs").resolve())])
            view = api._views[str((self.home / "breadcrumbs").resolve())][0]
            self.assertEqual(client.get(PREFIX + "/graph").status_code, 200)  # store "": the same View
            self.assertIs(api._views[str((self.home / "breadcrumbs").resolve())][0], view)
            self.assertFalse(api.warm("nosuchprofile"))
        finally:
            api.close_all()


@unittest.skipIf(FASTAPI, FASTAPI)
class ExtraStores(Contract):
    """A store outside the profiles (an A/B arm) is named in stores.local.json."""

    def test_a_named_store_outside_the_profiles(self):
        arm = Path(self.tmp.name) / "ab" / "off"
        arm.mkdir(parents=True)
        p, _ = SCENARIO(str(arm))
        p.shutdown()
        extra = Path(self.tmp.name) / "stores.local.json"
        extra.write_text(json.dumps({"ab-off": str(arm / "breadcrumbs"), "demo": "/elsewhere", "rel": "a/b",
                                     "default": "/elsewhere", "../x": str(arm / "breadcrumbs")}))
        api, client = plugin_client(self.home, self.tmp.name, extra)
        try:
            got = client.get(PREFIX + "/context", params={"sid": "s3", "store": "ab-off"})
            self.assertEqual(got.json(), self.fixture["context_s3"])
            listed = {s["store"]: s for s in client.get(PREFIX + "/stores").json()["stores"]}
            self.assertEqual(sorted(listed), ["ab-off", "demo"])  # rel, default and ../x are left out
            self.assertEqual((listed["ab-off"]["extra"], listed["demo"]["extra"]), (True, False))
            self.assertEqual(Path(listed["demo"]["root"]), self.root)  # a profile name keeps its profile
            self.assertEqual(client.get(PREFIX + "/context", params={"sid": "s3", "store": "rel"}).status_code, 404)
            extra.write_text("{not json")
            self.assertEqual(client.get(PREFIX + "/context", params={"sid": "s3", "store": "ab-off"}).status_code, 500)
        finally:
            api.close_all()


@unittest.skipIf(FASTAPI, FASTAPI)
class HermesLoader(unittest.TestCase):
    def test_the_dashboard_loader_mounts_the_router(self):
        try:
            from fastapi import FastAPI
            from hermes_cli import web_server_dashboard as dash
        except ImportError as e:
            self.skipTest("not the Python of a Hermes install: %s" % e)
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        plugin = dash._dashboard_plugin_entry(manifest, manifest["name"], ROOT / "dashboard", "user")
        self.assertEqual((plugin["name"], plugin["has_api"], plugin["_api_file"]), ("breadcrumbs", True, "plugin_api.py"))
        app = FastAPI()
        with dash._plugin_api_mount_lock:
            self.assertTrue(dash._mount_one_plugin_api(app, plugin, {"breadcrumbs"}, set()))
        paths = {getattr(r, "path", "") for r in app.routes}
        self.assertTrue(PREFIX + "/{name}" in paths or PREFIX + "/{path:path}" in paths, paths)
        with dash._plugin_api_mount_lock:  # not in plugins.enabled: the loader must not import it
            self.assertFalse(dash._mount_one_plugin_api(FastAPI(), plugin, set(), set()))


if __name__ == "__main__":
    unittest.main()
