"""G03: the data of the live explorer. What the plugin routes give for the seeded demo store, as JSON files.

    python3 -B make-data.py PIN OUT

PIN is a checkout of the plugin (read only; run with -B so that no .pyc is written there). OUT is the data folder.
The store is the pin's own demo/seed.py (fixed times, profile bcdemo, no model, no network), made in a scratch
folder that this script removes at the end. The routes are dashboard/plugin_api.py of the pin, called as the stage
server of P01 calls them: same module, same fixed clock, pointed at the scratch root. No other store is read.

Files (OUT/): stores.json, graph.json, session/<sid>.json, context/<sid>.json, venn/<sid>-<at>.json, crumbs.json
(each crumb of the views at the levels 0, 0.5 and 1: the route GET /crumb). The scratch path is blanked in stores.json.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

sys.dont_write_bytecode = True
os.environ["SPACEGRAPH_NO_WARM"] = "1"  # no warm-up thread at import
NOW = 1_794_100_000  # the fixed clock of the heat values, as in the stage server of P01
STORE = "bcdemo"
LEVELS = ("0", "0.5", "1")

if len(sys.argv) != 3:
    sys.exit(__doc__)
pin, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
work = Path(tempfile.mkdtemp(prefix="g03-"))
try:
    root = work / "hermes"
    home = root / "profiles" / STORE
    home.mkdir(parents=True)
    env = {**os.environ, "BREADCRUMBS_SUMMARIZE": "0", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-B", str(pin / "demo" / "seed.py"), str(home)], env=env, check=True,
                   stdout=subprocess.DEVNULL)

    spec = importlib.util.spec_from_file_location("bc_plugin_api_g03", pin / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    setattr(api, "hermes_root", lambda: root)  # setattr: the module is loaded by path, and pointed at the scratch root
    setattr(api, "active_home", lambda: home)
    setattr(api, "clock", lambda: NOW)
    setattr(api, "EXTRA_FILE", root / "no-stores.local.json")

    def route(name, **q):
        body = api.api(name, types.SimpleNamespace(query_params={"store": STORE, **q}))
        if hasattr(body, "body"):  # a JSONResponse: an error
            raise RuntimeError("%s %s: %s" % (name, q, body.body.decode()))
        return body

    files = {}

    stores = api.stores()
    assert [s["store"] for s in stores["stores"]] == [STORE], stores
    for s in stores["stores"]:
        s["root"] = ""  # the scratch folder of this run: not data
    files["stores.json"] = stores

    graph = route("graph")
    files["graph.json"] = graph
    sids = [n["session"] for n in graph["nodes"] if n["kind"] == "sess"]
    ids = {n["id"] for n in graph["nodes"]}
    for sid in sorted(sids):
        view = route("session", sid=sid)
        files["session/%s.json" % sid] = view
        ids.update(n["id"] for n in view["nodes"])
        ctx = route("context", sid=sid)
        files["context/%s.json" % sid] = ctx
        for at in range(len(ctx["events"])):
            files["venn/%s-%d.json" % (sid, at)] = route("venn", sid=sid, at=str(at))
    files["crumbs.json"] = {i: {lv: route("crumb", id=i, level=lv) for lv in LEVELS} for i in sorted(ids)}

    text = {name: json.dumps(body, ensure_ascii=False, separators=(",", ":")) for name, body in files.items()}
    paths = (str(work), "/Use" + "rs/", "file:" + "//")  # written in two parts: this file must not hold them either
    bad = [n for n, t in text.items() if any(p in t for p in paths)]
    if bad:
        sys.exit("a path of this machine is in: %s" % ", ".join(bad))
    if out.exists():
        if not (out / "stores.json").is_file():
            sys.exit("%s exists and is not a data folder of this script (no stores.json): not removed" % out.name)
        shutil.rmtree(out)
    for name, t in text.items():
        p = out / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t + "\n", encoding="utf-8")
    print("%d files, %d bytes in %s sessions; %d crumbs at %d levels" % (
        len(text), sum(len(t) + 1 for t in text.values()), len(sids), len(ids), len(LEVELS)))
    api.close_all()
finally:
    shutil.rmtree(work, ignore_errors=True)
