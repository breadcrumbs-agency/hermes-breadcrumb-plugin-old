"""Command line: python -m breadcrumbs [--home DIR] COMMAND ...

  bc REF [LEVEL]            show a breadcrumb at a level 0..1 (default 0.25)
  find QUERY [K]            search; prints handles with their best line
  log [REF]                 commits on a ref (default main), newest first
  stats                     blobs, bytes on disk, raw bytes, ratios
  verify                    re-read every blob and check every CRC32C
  summarize [REF...] [--all]  small-model summaries (--all: every session and closed span)
  publish REF [--deep]      make a public copy of a crumb (the original stays private)
  share REF... --out FILE   export the public crumbs reachable from REF as an IPFS CAR file
  import FILE               import a CAR file as local public crumbs
  project NAME [REF...]     add public copies of crumbs to a shared project (no REF: list its heads)
  push NAME [--car FILE]    write the project head as a CAR, add it to IPFS (kubo), print its CID
  pull NAME CID|FILE        fetch a project head from IPFS (or read a CAR file) and join it
  ingest-hermes PROFILE     backfill a profile's state.db (opened read-only)
  prune-index               drop index rows that older versions made for crumb/crumb_find calls
  install-hermes PROFILE    write the plugin shim into the profile
  sources PROFILE           the codebases in the profile's tool calls: calls, chats, last use (islands)
  term [--url URL] [--every S]  the explorer page drawn in Kitty or Ghostty (Kitty graphics protocol)
  core mcp|sock|a2a|setup [HARNESS]  the one read-only core of every harness (find, open, zoom, trail, stores, health):
                            mcp: MCP over stdio; sock: Unix socket (--tcp PORT adds loopback TCP); a2a: A2A on 127.0.0.1;
                            setup: the command that adds it to hermes, claude-code, codex or opencode
  open [web|kitty|ghostty] [--print]  the explorer in the browser (default), or in a new Kitty or Ghostty window that
                            runs term; --print shows the command line and runs nothing; --port P: the web port (8765)
  slot PROFILE [PROVIDER]   run the profile's memory provider (or PROVIDER) THRU breadcrumbs (--off: undo)
  seats PROFILE             the other memory systems: the way each one is taken in, where it is, its data
  seat clone NAME PROFILE   copy a provider that Hermes took out of its core from Hermes's git history
  seat snap NAME PROFILE    keep the exact bytes of a system's state files (--path adds more)
  seat harvest NAME PROFILE keep each record that a system stores as a searchable crumb
  seat restore NAME|ID PROFILE  write a snap's files back (--force replaces files that are there)
  seat replay NAME PROFILE --from REF  give the kept texts of REF to the system NAME (Hermes's Python)
  hive serve [NAME=DIR...] [--all NAME]  serve stores read-only on 127.0.0.1:4700 (public crumbs; --all: whole)
  hive up [--port P]        kubo: listen for the hive on P (and Hermes A2A), forward to each peer of config.json "hive"
  hive find QUERY [K] [--ms MS]  search the hive peers; a peer that cannot answer in MS passes
  hive open ID@PEER/STORE [LEVEL]  show a remote breadcrumb (each part is checked)
  hive card                 each peer: its stores, capacity and counts
  vault export DIR [--all]  write an Obsidian vault: one note per session (--all: private crumbs too)
  vault import DIR          read an Obsidian vault: one crumb per note, its exact bytes; [[links]] link crumbs
  migrate SYSTEM FILE       bring another memory system's export in, with its ids (chatgpt, claude, hermes,
                            mem0, openmemory, supermemory, hindsight, honcho, retaindb: see migrate.py)

PROFILE is a profile name or a HERMES_HOME dir (a path).
--home defaults to $BREADCRUMBS_HOME, else $HERMES_HOME/breadcrumbs, else ~/.hermes/breadcrumbs.
--graal runs the command under GraalPy (see breadcrumbs/runtime.py) when one is found.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SHIM = '''r"""breadcrumbs memory provider (plugin shim, written by `python -m breadcrumbs install-hermes`).

Source: {repo}
"""
import sys

_REPO = {repo!r}
_NAME = {name!r}
_DEV = {dev!r}  # True: import the repo code fresh at every agent build (long-lived gateways)


def _provider_class():
    if _REPO not in sys.path:
        sys.path.insert(0, _REPO)
    if _DEV:
        for mod in [m for m in list(sys.modules) if m == "breadcrumbs" or m.startswith("breadcrumbs.")]:
            sys.modules.pop(mod, None)
    from breadcrumbs.hermes import BreadcrumbsProvider
    return BreadcrumbsProvider


def register(ctx):
    # Hermes finds a plugin's memory provider and context engine by these two calls in this file's text
    provider = _provider_class()(name=_NAME)  # first: with _DEV it reloads the package
    ctx.register_memory_provider(provider)
    from breadcrumbs import ace
    from breadcrumbs.hermes import register_plugin  # the handbook, the intercept and the islands
    register_plugin(ctx, provider)
    if hasattr(ctx, "register_context_engine") and ace.wanted(name=_NAME):
        ctx.register_context_engine(ace.Ace(name=_NAME))
'''

PLUGIN_YAML = """name: {name}
version: 0.0.1
description: Content-addressed breadcrumb memory with exact provenance and zoom levels 0..1.
"""


def profile_home(profile):
    root = os.path.expanduser("~/.hermes")
    return root if profile in ("", "default") else os.path.join(root, "profiles", profile)


def default_home():
    if os.environ.get("BREADCRUMBS_HOME"):
        return os.environ["BREADCRUMBS_HOME"]
    return os.path.join(os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"), "breadcrumbs")


def hermes_home(arg):
    """A profile name, or a HERMES_HOME dir given as a path."""
    if os.sep in arg or "/" in arg or arg.startswith(("~", ".")):
        return os.path.abspath(os.path.expanduser(arg))
    return profile_home(arg)


def open_all(home):
    from .history import Refs
    from .index import Index
    from .store import Store
    from .zoom import Zoom
    store = Store(os.path.join(home, "store"))
    index = Index(os.path.join(home, "index.db"))
    return store, index, Refs(os.path.join(home, "refs")), Zoom(store, cues=index.signature, words=index.tiers)


def hermes_config(profile, *args):
    """`hermes -p PROFILE config ARGS...`: the last line of its output (Hermes's own notices dropped)."""
    p = subprocess.run(["hermes", "-p", profile or "default", "config", *args], capture_output=True, text=True,
                       timeout=120)
    if p.returncode:
        if args[:1] == ("get",) and "not set" in (p.stderr or "") + (p.stdout or ""):
            return ""  # Hermes exits 1 on a key that is not set: that is no value, not a failure
        raise SystemExit("breadcrumbs: hermes config %s failed: %s" % (" ".join(args), (p.stderr or p.stdout)[-300:]))
    lines = [line.strip() for line in p.stdout.splitlines() if line.strip() and not line.startswith("hermes:")]
    return lines[-1] if lines else ""


NO_PROVIDER = {"", "none", "null", "builtin", "(not set)"}


def slot_command(args):
    """The pass-through installer: the profile's memory provider (or args.provider) runs in breadcrumbs' slot and
    breadcrumbs takes its place; --off gives the slot its place back. Other settings in config.json stay."""
    root = os.path.join(profile_home(args.profile), "breadcrumbs")
    path = os.path.join(root, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    current = hermes_config(args.profile, "get", "memory.provider")
    if args.off:
        old = cfg.pop("slot", "")
        if not old:
            print("%s: no slot" % args.profile)
            return 0
        _write_json(path, cfg)
        hermes_config(args.profile, "set", "memory.provider", old)
        print("%s: %s is the memory provider again. breadcrumbs keeps the copies it made." % (args.profile, old))
        return 0
    name = args.provider or ("" if current.lower() in NO_PROVIDER or current == args.name else current)
    if not name:
        print("%s: no slot (memory provider: %s)%s" % (args.profile, current or "none",
                                                     "; the slot holds " + cfg["slot"] if cfg.get("slot") else ""))
        return 0
    cfg["slot"] = name
    _write_json(path, cfg)
    if current != args.name:
        hermes_config(args.profile, "set", "memory.provider", args.name)
    print("%s: %s runs in breadcrumbs' slot, and %s is the memory provider. A gateway restart loads it.\n"
          "undo: python -m breadcrumbs slot %s --off" % (args.profile, name, args.name, args.profile))
    return 0


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def ipfs(*args, stdout=subprocess.DEVNULL, timeout=300):
    """Run kubo (the ipfs binary; BREADCRUMBS_IPFS overrides the PATH) with the user's IPFS_PATH."""
    exe = os.environ.get("BREADCRUMBS_IPFS") or shutil.which("ipfs")
    if not exe:
        raise SystemExit("breadcrumbs: no ipfs binary (kubo); install it, set BREADCRUMBS_IPFS, or use --no-ipfs")
    p = subprocess.run([exe, "--timeout", "%ds" % timeout, *args], stdout=stdout, stderr=subprocess.PIPE,
                       timeout=timeout + 30)
    if p.returncode:
        raise SystemExit("breadcrumbs: ipfs %s failed: %s" % (args[0], p.stderr.decode("utf-8", "replace")[-300:]))


def hive_stores(specs, whole):
    """NAME=DIR specs -> {name: (dir, scope)}: scope "all" for the names in whole, else "shared"."""
    out = {}
    for spec in specs:
        name, eq, root = spec.partition("=")
        if not (name and eq and root):
            raise SystemExit("breadcrumbs: a hive store is NAME=DIR, not %r" % spec)
        out[name] = (os.path.abspath(os.path.expanduser(root)), "all" if name in whole else "shared")
    return out


def hive_main(args, home):
    from . import hive
    if args.hcmd == "serve":
        stores = hive_stores(args.stores or ["main=" + home], args.all)
        server = hive.Server(stores, port=args.port, workers=args.workers, depth=args.depth,
                             max_ms=args.max_ms).start()
        print(json.dumps({"hive": "127.0.0.1:%d" % server.port, "proto": hive.PROTO, "stores": stores}), flush=True)
        try:
            while True:
                time.sleep(1)  # a sleep loop: Ctrl-C works on Windows too
        except KeyboardInterrupt:
            pass
        server.close()
        return 0
    try:
        with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f).get("hive") or {}
    except (OSError, ValueError, AttributeError):
        cfg = {}
    if args.hcmd == "up":  # kubo listeners for this host, and forwards to each peer
        ports = {hive.PROTO: args.port, hive.A2A: cfg.get("a2a")}  # a2a: this host's Hermes A2A port
        for proto, port in ports.items():
            ipfs("p2p", "close", "-p", proto, timeout=10)
            if port:
                ipfs("p2p", "listen", proto, "/ip4/127.0.0.1/tcp/%d" % int(port), timeout=10)
        for name, peer in sorted((cfg.get("peers") or {}).items()):
            for proto, key in ((hive.PROTO, "addr"), (hive.A2A, "a2a")):
                if peer.get("peer") and peer.get(key):
                    port = int(peer[key].rpartition(":")[2])
                    ipfs("p2p", "forward", proto, "/ip4/127.0.0.1/tcp/%d" % port, "/p2p/" + peer["peer"], timeout=10)
                    print("forward", name, proto, "127.0.0.1:%d" % port)
        return 0
    h = hive.Hive.from_config(cfg)
    if h is None:
        raise SystemExit("breadcrumbs: no hive peers in %s" % os.path.join(home, "config.json"))
    try:
        if args.hcmd == "find":
            ms = h.ms if args.ms is None else args.ms
            print(hive.render(h.ask({"op": "find", "q": args.query, "k": args.k}, ms), args.k, ms)[0], end="")
        elif args.hcmd == "open":
            ref, peer, store = hive.split(args.ref)
            print(h.open(peer, store, ref, args.level)[0], end="")
        elif args.hcmd == "card":
            print(json.dumps(h.ask({"op": "card"}, max(h.ms, 1000.0)), indent=1))
    finally:
        h.close()
    return 0


def dir_bytes(path):
    total = 0
    for base, _dirs, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(base, f)) for f in files)
    return total


def core_command(args, home):
    """`core mcp|sock|a2a|setup`: the servers of core.py. All are read-only. `home` is --home (None: the default only)."""
    from . import core as corelib
    stores = corelib.Stores(local=home or os.environ.get("BREADCRUMBS_HOME"))
    if args.mode == "setup":
        names = [args.harness] if args.harness else list(corelib.HARNESSES)
        if any(n not in corelib.HARNESSES for n in names):
            print("breadcrumbs: harness is one of %s" % ", ".join(corelib.HARNESSES), file=sys.stderr)
            return 2
        for n in names:
            print("%s:\n%s\n" % (corelib.HARNESSES[n]["label"], corelib.HARNESSES[n]["setup"](corelib.server_command("mcp"))))
        return 0
    core = corelib.Core(stores)
    path = None
    key = lambda: corelib.token(os.path.join(stores.base, "core.token"))  # noqa: E731
    try:
        if args.mode == "mcp":
            from . import mcp
            return mcp.serve(core)
        if args.mode == "a2a":
            from . import a2a
            server = a2a.make_server(core, args.port, key())
            print("breadcrumbs core a2a: http://127.0.0.1:%d/.well-known/agent-card.json (token: %s)" % (
                server.server_address[1], os.path.join(stores.base, "core.token")), file=sys.stderr, flush=True)
            servers = [server]
        else:
            from . import sock
            path = args.unix or sock.default_path(core)
            servers: list = [sock.make_unix(core, path)]
            print("breadcrumbs core sock: %s" % path, file=sys.stderr, flush=True)
            if args.tcp is not None:
                servers.append(sock.make_tcp(core, args.tcp, key()))
                print("breadcrumbs core sock: 127.0.0.1:%d (token: %s)" % (
                    servers[-1].server_address[1], os.path.join(stores.base, "core.token")), file=sys.stderr, flush=True)
        import threading
        for s in servers[1:]:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            servers[0].serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for s in servers:
                s.server_close()
        if args.mode == "sock" and path and os.path.exists(path):
            os.unlink(path)
        return 0
    except OSError as e:
        print("breadcrumbs core: %s" % e, file=sys.stderr)
        return 1
    finally:
        core.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="breadcrumbs", description=__doc__.splitlines()[0])
    ap.add_argument("--home", default=None)
    ap.add_argument("--graal", action="store_true", help="run under GraalPy when it is available")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("bc"); p.add_argument("ref"); p.add_argument("level", nargs="?", type=float, default=0.25)
    p = sub.add_parser("find"); p.add_argument("query"); p.add_argument("k", nargs="?", type=int, default=8)
    p = sub.add_parser("log"); p.add_argument("ref", nargs="?", default="main")
    sub.add_parser("ace", help="the size of each chat against its ACE target (from ace.log; read-only)")
    p = sub.add_parser("dials", help="every knob and dial with its value and where the value came from (read-only)")
    p.add_argument("--model", help="the model whose entries of config.json count")
    p.add_argument("--markdown", action="store_true", help="the tables of README \"Knobs and dials\"")
    sub.add_parser("stats")
    sub.add_parser("verify")
    p = sub.add_parser("summarize"); p.add_argument("refs", nargs="*"); p.add_argument("--all", action="store_true")
    p = sub.add_parser("publish"); p.add_argument("ref"); p.add_argument("--deep", action="store_true")
    p = sub.add_parser("share"); p.add_argument("refs", nargs="+"); p.add_argument("--out", required=True)
    p = sub.add_parser("import"); p.add_argument("car")
    p = sub.add_parser("project"); p.add_argument("name"); p.add_argument("refs", nargs="*")
    p = sub.add_parser("push"); p.add_argument("name"); p.add_argument("--car")
    p.add_argument("--no-ipfs", action="store_true", help="write the CAR file only")
    p = sub.add_parser("pull"); p.add_argument("name"); p.add_argument("source", help="a CID or a CAR file")
    p = sub.add_parser("ingest-hermes"); p.add_argument("profile")
    sub.add_parser("prune-index")
    p = sub.add_parser("supersede", help="tag crumb OLD as superseded by crumb NEW (--undo: retract the tag)")
    p.add_argument("old"); p.add_argument("new"); p.add_argument("--undo", action="store_true")
    p = sub.add_parser("migrate", help="bring another memory system's export in, with its ids")
    p.add_argument("system", choices=("chatgpt", "claude", "hermes", "mem0", "openmemory", "supermemory",
                                      "hindsight", "honcho", "retaindb")); p.add_argument("file")
    p = sub.add_parser("vault", help="the memory as an Obsidian vault (export), a vault as crumbs (import)")
    p.add_argument("action", choices=("export", "import")); p.add_argument("dir")
    p.add_argument("--all", action="store_true", help="export: the private crumbs too")
    p = sub.add_parser("slot", help="run another memory provider THRU breadcrumbs (a pass-through)")
    p.add_argument("profile"); p.add_argument("provider", nargs="?", default="")
    p.add_argument("--off", action="store_true", help="give the slot its place back as the memory provider")
    p.add_argument("--name", default="breadcrumbs", help="breadcrumbs' provider name in this profile")
    p = sub.add_parser("install-hermes"); p.add_argument("profile")
    p.add_argument("--name", default="breadcrumbs", help="provider and plugin dir name")
    p.add_argument("--dev", action="store_true", help="re-import the repo code at every agent build")
    p = sub.add_parser("sources", help="the codebases in a profile's tool calls (islands)")
    p.add_argument("profile")
    p = sub.add_parser("serve", help="the explorer page on 127.0.0.1 (read-only)")
    p.add_argument("--port", type=int, default=8765); p.add_argument("--open", action="store_true")
    p = sub.add_parser("open", help="the explorer in the default browser, or in a new Kitty or Ghostty window")
    p.add_argument("where", nargs="?", choices=("web", "kitty", "ghostty"), default="web")
    p.add_argument("--print", dest="show", action="store_true", help="show the command line that would run; run nothing")
    p.add_argument("--port", type=int, default=None, help="web: the port of the server (default 8765, else a free port)")
    p = sub.add_parser("viz", help="open the explorer here: a cmux split, else Kitty/Ghostty, else the browser")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("core", help="the one core of every harness: MCP, A2A or a socket (see breadcrumbs/core.py)")
    p.add_argument("mode", choices=("mcp", "sock", "a2a", "setup"))
    p.add_argument("harness", nargs="?", help="setup: hermes, claude-code, codex or opencode (default: all)")
    p.add_argument("--unix", default=None, help="sock: the socket path (default: core.sock next to the default store)")
    p.add_argument("--tcp", type=int, default=None, help="sock: also listen on 127.0.0.1:PORT (needs the token)")
    p.add_argument("--port", type=int, default=4701, help="a2a: the port on 127.0.0.1")
    sub.add_parser("help", help="show this list")
    p = sub.add_parser("term", help="the explorer page in Kitty or Ghostty (Kitty graphics protocol)")
    p.add_argument("--url", default=None, help="default: serve --home on a free port")
    p.add_argument("--every", type=float, default=10.0, help="seconds per frame")
    p.add_argument("--width", type=int, default=1280)
    p.add_argument("--height", type=int, default=800)
    p.add_argument("--wait-ms", type=int, default=8000, help="page time for its API calls (Chrome virtual time)")
    p.add_argument("--frames", type=int, default=0, help="stop after N frames (0: until Ctrl-C)")
    p = sub.add_parser("spacegraph", help="native SpaceGraph viewer in Kitty/Ghostty (no Chrome)")
    p.add_argument("--port", type=int, default=8765, help="port for the view API server"); p.add_argument("--motion", choices=("0", "1"), default=None, help="0: the camera of the old page (no momentum); or BREADCRUMBS_MOTION=0")
    p = sub.add_parser("seats"); p.add_argument("profile"); p.add_argument("--repo", default=None)
    p = sub.add_parser("seat"); p.add_argument("action", choices=("clone", "snap", "harvest", "restore", "replay"))
    p.add_argument("name"); p.add_argument("profile"); p.add_argument("--repo", default=None)
    p.add_argument("--path", action="append", default=[]); p.add_argument("--from", dest="src", default="memory")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("hive", help="search other hosts within a time budget (see breadcrumbs/hive.py)")
    hs = p.add_subparsers(dest="hcmd", required=True)
    q = hs.add_parser("serve"); q.add_argument("stores", nargs="*", help="NAME=DIR (default: main=--home)")
    q.add_argument("--all", action="append", default=[], help="serve this store whole, not only its public crumbs")
    q.add_argument("--port", type=int, default=4700); q.add_argument("--workers", type=int, default=2)
    q.add_argument("--depth", type=int, default=32); q.add_argument("--max-ms", type=float, default=2000)
    q = hs.add_parser("up"); q.add_argument("--port", type=int, default=4700)
    q = hs.add_parser("find"); q.add_argument("query"); q.add_argument("k", nargs="?", type=int, default=8)
    q.add_argument("--ms", type=float, default=None)
    q = hs.add_parser("open"); q.add_argument("ref"); q.add_argument("level", nargs="?", type=float, default=0.25)
    hs.add_parser("card")
    p = sub.add_parser("no-vis")  # private: no help line, and the list above leaves it out (see breadcrumbs/novis.py)
    p.add_argument("action", nargs="?", choices=("list", "find", "add", "rm"), default="list")
    p.add_argument("names", nargs="*")
    sub.metavar = "{%s}" % ",".join(n for n in sub.choices if n != "no-vis")
    cli_args = list(sys.argv[1:] if argv is None else argv)
    args = ap.parse_args(cli_args)
    home = args.home or default_home()
    if args.graal and sys.implementation.name != "graalpy":
        from .runtime import find_graalpy, graal_argv
        exe = find_graalpy()
        if exe:
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (REPO, os.environ.get("PYTHONPATH")) if p))
            sys.stdout.flush()
            os.execve(exe, graal_argv(exe, [a for a in cli_args if a != "--graal"]), env)
        print("breadcrumbs: no GraalPy found; running on %s" % sys.implementation.name, file=sys.stderr)

    if args.cmd == "slot":
        return slot_command(args)
    if args.cmd == "no-vis":
        from . import novis
        return novis._command(args.action, args.names)
    if args.cmd in ("serve", "viz", "open", "term", "spacegraph") and not getattr(args, "url", None):
        from . import novis
        why = novis.blocked(os.path.dirname(os.path.abspath(home)))  # a profile that opted out of visualization
        if why:
            print("breadcrumbs: %s" % why, file=sys.stderr)
            return 1
    if args.cmd == "core":
        return core_command(args, args.home)
    if args.cmd == "install-hermes":
        target = os.path.join(profile_home(args.profile), "plugins", args.name)
        shim = os.path.join(target, "__init__.py")
        if os.path.exists(shim):
            with open(shim, encoding="utf-8") as f:
                if "plugin shim, written by" not in f.read():
                    print("breadcrumbs: %s holds a plugin this command did not write (a catalog install?); "
                          "not overwriting it. Use --name for a dev shim." % target, file=sys.stderr)
                    return 1
        os.makedirs(target, exist_ok=True)
        with open(shim, "w", encoding="utf-8") as f:
            f.write(SHIM.format(repo=REPO, name=args.name, dev=args.dev))
        with open(os.path.join(target, "plugin.yaml"), "w", encoding="utf-8") as f:
            f.write(PLUGIN_YAML.format(name=args.name))
        print("wrote", target)
        print("activate: hermes -p %s config set memory.provider %s   (undo: hermes -p %s memory off)"
              % (args.profile, args.name, args.profile))
        return 0
    if args.cmd == "sources":
        from . import island
        home = hermes_home(args.profile)
        own = island.own_root(home)
        for root, (calls, chats, last) in sorted(island.scan(home).items(), key=lambda kv: -kv[1][0]):
            print("%7d calls %5d chats  %s  %s%s" % (calls, chats, time.strftime("%Y-%m-%d", time.localtime(last)),
                                                   island.short(root), "  (the island of this profile)" * (root == own)))
        return 0

    if args.cmd == "hive":
        return hive_main(args, home)

    if args.cmd == "open":
        from . import launch
        return launch.run(args.where, home, port=args.port, show=args.show)

    if args.cmd == "serve":
        from .view import make_server, token
        server = make_server(home, port=args.port)
        url = "http://127.0.0.1:%d/#k=%s" % (server.server_address[1], token(home))
        print("breadcrumbs explorer (read-only):", url, flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "help":
        ap.print_help()
        return 0

    if args.cmd == "viz":
        from . import viz
        return viz.run(home, port=args.port)

    if args.cmd == "term":
        from . import term
        server = None
        if args.url is None:
            import threading
            from .view import make_server, token
            server = make_server(home, port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            args.url = "http://127.0.0.1:%d/#k=%s&motion=0" % (server.server_address[1], token(home))  # one picture: the camera of before
        try:
            return term.run(args.url, every=args.every, width=args.width, height=args.height,
                            budget_ms=args.wait_ms, frames=args.frames)
        finally:
            if server:
                server.shutdown()
                server.server_close()

    if args.cmd == "spacegraph":
        from .spacegraph_kitty import SpaceGraphKitty
        app = SpaceGraphKitty(home, port=args.port, motion=args.motion)
        app.run()
        return 0

    if args.cmd == "seats" or args.cmd == "seat" and args.action == "clone":  # no store needed
        from . import seats
        hh, repo = hermes_home(args.profile), args.repo or seats.HERMES_REPO
        if args.cmd == "seats":
            for r in seats.survey(hh, repo):
                print("%-14s %-7s %-20s %d" % (r["name"], r["way"], ",".join(r["where"]) or "-", r["data"]))
            return 0
        try:
            path = seats.clone(args.name, os.path.join(hh, "plugins"), repo)
        except (LookupError, FileExistsError) as e:
            print("breadcrumbs: %s" % e, file=sys.stderr)
            return 1
        with open(os.path.join(path, "CLONED.json"), encoding="utf-8") as f:
            meta = json.load(f)
        print("cloned %s from %s at %s into %s" % (args.name, meta["repo"], meta["commit"], path))
        return 0

    if args.cmd == "dials":  # read-only
        from . import dials
        print(dials.markdown() if args.markdown else "\n".join(dials.show(home, args.model)))
        return 0

    if args.cmd == "ace":  # read-only
        from . import ace
        lines = ace.status_lines(home)
        print("\n".join(lines) if lines else "no ace.log at %s" % home)
        return 0 if lines else 1

    if args.cmd == "bc":  # read-only, like the View: safe while a gateway runs (the dispatch handbook)
        from .view import View
        try:
            v = View(home)
        except FileNotFoundError:
            print("no store at %s" % home, file=sys.stderr)
            return 1
        try:
            sys.stdout.write(v.crumb(args.ref, args.level)["text"])
        except (KeyError, ValueError):
            print("no crumb %s in %s" % (args.ref, home), file=sys.stderr)
            return 1
        finally:
            v.close()
        return 0

    if args.cmd == "vault" and args.action == "export":  # read-only, like bc
        from .vault import export_vault
        from .view import View
        try:
            v = View(home)
        except FileNotFoundError:
            print("no store at %s" % home, file=sys.stderr)
            return 1
        try:
            print(json.dumps(export_vault(v, args.dir, everything=args.all)))
        except FileExistsError as e:
            print("breadcrumbs: %s" % e, file=sys.stderr)
            return 1
        finally:
            v.close()
        return 0

    store, index, refs, zoom = open_all(home)
    try:
        if args.cmd == "seat":
            from . import seats
            hh = hermes_home(args.profile)
            try:
                if args.action == "snap":
                    sid = seats.snap(store, refs, args.name, hh, seats.state_paths(args.name, hh) + args.path)
                    print(json.dumps({"seat": args.name, "snap": sid}))
                    return 0 if sid else 1
                if args.action == "harvest":
                    got, new = seats.harvest(store, index, refs, args.name, hh)
                    print(json.dumps({"seat": args.name, "records": got, "new": len(new)}))
                elif args.action == "restore":
                    sid = seats.snap_of(store, refs, args.name)
                    print(json.dumps({"snap": sid, "written": seats.restore(store, sid, hh, force=args.force)}))
                else:  # replay
                    provider = seats.load_provider(args.name, hh)
                    try:
                        sent = seats.replay(store, refs, args.src, provider, args.name)
                    finally:
                        getattr(provider, "shutdown", lambda: None)()
                    print(json.dumps({"seat": args.name, "from": args.src, "sent": len(sent)}))
            except (LookupError, FileExistsError, ValueError) as e:
                print("breadcrumbs: %s" % e, file=sys.stderr)
                return 1
            return 0
        if args.cmd == "find":
            from .hermes import BreadcrumbsProvider
            prov = BreadcrumbsProvider()
            prov.store, prov.index, prov.refs, prov.zoom = store, index, refs, zoom  # find names sessions by ref
            sys.stdout.write(prov.find(args.query, args.k))
        elif args.cmd == "log":
            from .history import log
            for cid, c in log(store, refs.get(args.ref)):
                print(cid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(c.meta.get("ts", 0))),
                      len(c.kids), "roots", c.meta.get("msg", ""))
        elif args.cmd == "stats":
            store.flush()
            raw = sum(store._len)
            pack = os.path.getsize(os.path.join(home, "store", "blobs.pack"))
            from .crc32c import BACKEND
            out = {"home": home, "runtime": sys.implementation.name, "crc32c": BACKEND,
                   "blobs": len(store), "unique_raw_bytes": raw, "pack_bytes": pack,
                   "pack_ratio": round(raw / pack, 2) if pack else None,
                   "store_bytes": dir_bytes(os.path.join(home, "store")),
                   "index_bytes": os.path.getsize(os.path.join(home, "index.db")),
                   "refs": len(refs.names())}
            print(json.dumps(out, indent=1))
        elif args.cmd == "verify":
            t0, n, bad = time.perf_counter(), 0, 0
            for blob_id in list(store.ids()):
                try:
                    store.get(blob_id)
                except ValueError:
                    bad += 1
                n += 1
            print(json.dumps({"blobs": n, "bad": bad, "seconds": round(time.perf_counter() - t0, 3)}))
            return 1 if bad else 0
        elif args.cmd == "summarize":
            from . import summarize as sm
            from .history import commit
            runner = sm.Llama()
            if hasattr(runner, "available") and not runner.available():
                print("breadcrumbs: no model: set BREADCRUMBS_SUM_MODEL and BREADCRUMBS_LLAMA", file=sys.stderr)
                return 2
            targets = [refs.get(r) or r for r in args.refs]
            if args.all:  # innermost spans first, so a session can use its span summaries
                for name in sorted(refs.names()):
                    if name.startswith("sess-"):
                        targets += sm.closed_spans(store, refs.get(name)) + [refs.get(name)]
            made, failed, t0 = [], 0, time.perf_counter()
            for cid in dict.fromkeys(targets):
                had = index.summary_of(cid)
                try:
                    sid = sm.summarize(store, index, cid, runner)
                except Exception as e:  # one bad summary must not stop the rest
                    failed += 1
                    print("breadcrumbs: summary of %s failed: %s" % (cid, str(e)[-200:]), file=sys.stderr)
                    continue
                if sid and not had:
                    made.append(sid)
                    store.flush()
                    index.commit()
            if made:
                commit(store, refs, "summaries", made, "summarize %d" % len(made))
            print(json.dumps({"targets": len(dict.fromkeys(targets)), "made": len(made), "failed": failed,
                              "seconds": round(time.perf_counter() - t0, 1)}))
            return 1 if failed else 0
        elif args.cmd == "publish":
            from .share import publish
            print(publish(store, args.ref, args.deep))
        elif args.cmd == "share":
            from .share import export_car
            roots, stats = export_car(store, args.refs, args.out)
            print(json.dumps({"out": args.out, "roots": roots, **stats}))
        elif args.cmd == "import":
            from .history import commit
            from .share import import_car
            local = import_car(store, index, args.car)
            commit(store, refs, "import", local, "import " + os.path.basename(args.car))
            print(json.dumps({"roots": local}))
        elif args.cmd == "project":
            from .crumbs import get_crumb
            from .share import PROJ, project_add
            if args.refs:
                print(json.dumps({"project": args.name, "head": project_add(store, refs, args.name, args.refs)}))
            head = refs.get(PROJ + args.name)
            while head:  # the heads, newest first (first parents)
                c = get_crumb(store, head)
                print(json.dumps({"head": head, "cid": c.meta.get("cid"), "kids": len(c.kids),
                                  "joins": len(c.heritage) > 1}))
                head = c.heritage[0] if c.heritage else None
        elif args.cmd == "push":
            from .share import push
            path = args.car or os.path.join(home, "share", args.name + ".car")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            cid, head = push(store, refs, args.name, path)
            if not args.no_ipfs:
                ipfs("dag", "import", "--pin-roots", path)
            print(json.dumps({"project": args.name, "cid": cid, "head": head, "car": path}))
        elif args.cmd == "pull":
            from .share import pull
            src = args.source
            if not os.path.isfile(src):  # a CID: kubo fetches the blocks from its peers
                src = os.path.join(home, "share", args.source + ".car")
                os.makedirs(os.path.dirname(src), exist_ok=True)
                with open(src + ".tmp", "wb") as f:
                    ipfs("dag", "export", args.source, stdout=f)
                os.replace(src + ".tmp", src)
            print(json.dumps({"project": args.name, "head": pull(store, index, refs, args.name, src), "car": src}))
        elif args.cmd == "migrate":
            from .migrate import migrate_file
            print(json.dumps(migrate_file(store, index, refs, args.system, args.file)))
        elif args.cmd == "vault":  # import: export is read-only (above)
            from .vault import import_vault
            print(json.dumps(import_vault(store, index, refs, args.dir)))
        elif args.cmd == "supersede":
            from . import tags
            from .crumbs import get_crumb
            try:
                tag = tags.supersede(store, index, refs, args.old, args.new, undo=args.undo)
            except KeyError as e:
                print("breadcrumbs: no crumb %s in %s" % (e.args[0] if e.args else "", home), file=sys.stderr)
                return 1
            except ValueError as e:
                print("breadcrumbs: %s" % e, file=sys.stderr)
                return 1
            old, new = get_crumb(store, tag).heritage
            print(json.dumps({"tag": tag, "old": old, "new": new, "undo": args.undo}))
        elif args.cmd == "prune-index":
            from .ingest import prune_index
            n, rows = prune_index(store, index)
            print("pruned %d crumbs, %d index rows (calls and results of crumb and crumb_find; the crumbs stay)"
                  % (n, rows))
        elif args.cmd == "ingest-hermes":
            from .hermes_ingest import ingest_state_db
            db = os.path.join(profile_home(args.profile), "state.db")
            print(json.dumps(ingest_state_db(db, store, index, refs)))
    finally:
        index.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
