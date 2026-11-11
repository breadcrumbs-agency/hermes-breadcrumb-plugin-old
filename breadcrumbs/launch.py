"""`open`: the explorer in the default browser, or in a new Kitty or Ghostty window.

    python -m breadcrumbs [--home DIR] open [web|kitty|ghostty] [--print] [--port P]

web      The page on 127.0.0.1 in the default browser. A server on the port that takes this home's token is
         reused (it keeps the code it started with). Else one starts here, as `serve` does, and runs until
         Ctrl-C. The command prints the address without the token. The browser gets the token after the
         address (read from DIR/view.token, never printed).
kitty    A new Kitty window that runs `term` (the page drawn with the Kitty graphics protocol).
ghostty  The same in Ghostty.
--print  Show the command line that would run, and run nothing. It does not look for the app.

An app or Chrome that is not there is exit 2 and one line. Any other failure is exit 1 and one line.

How each app starts, from its makers:
  Kitty    `kitty [options] [program-to-run ...]`; on macOS `open -a kitty.app -n` (a new instance)
           https://sw.kovidgoyal.net/kitty/invocation/
  Ghostty  `ghostty -e PROGRAM ARGS...`; on macOS `open -na Ghostty.app --args -e PROGRAM ARGS...`
           https://ghostty.org/docs/config/reference (the -e flag); `ghostty --help` (macOS: "launching the
           terminal emulator from the CLI is not supported ... Use `open -na Ghostty.app` instead, or
           `open -na ghostty.app --args --foo=bar`"); the `ghostty` on PATH of a macOS shell is only that
           helper, and it starts no window
  open(1)  -n a new instance, -a the application, --args: the rest goes to the application (man open)
"""
import errno
import http.client
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

from . import term, view

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8765  # the port of `serve`
GRACE = 0.7  # seconds that a Linux app has to die before the command says that it started

# "mac": the name for `open -a`; "bin": the program on PATH; "flag": what goes before the program to run.
APPS = {
    "kitty": {"name": "Kitty", "mac": "kitty.app", "bin": "kitty", "flag": [],
              "brew": "brew install --cask kitty", "pkg": "apt install kitty (or your package manager)",
              "page": "https://sw.kovidgoyal.net/kitty/binary/"},
    "ghostty": {"name": "Ghostty", "mac": "Ghostty.app", "bin": "ghostty", "flag": ["-e"],
                "brew": "brew install --cask ghostty", "pkg": "your package manager",
                "page": "https://ghostty.org/download"},
}


def is_mac():
    return sys.platform == "darwin"


def term_argv(home):
    """`term` on home, found by Python wherever the window starts (a new window has another working folder)."""
    path = os.pathsep.join(p for p in (REPO, os.environ.get("PYTHONPATH")) if p)
    return ["env", "PYTHONPATH=" + path, sys.executable, "-m", "breadcrumbs", "--home", home, "term"]


def window_argv(app, home):
    """The command line that opens a new window of app that runs `term` on home."""
    a = APPS[app]
    if is_mac():
        return ["open", "-na", a["mac"], "--args", *a["flag"], *term_argv(home)]
    return [a["bin"], *a["flag"], *term_argv(home)]


def missing(app, err):
    a = APPS[app]
    what, how = (("%s is not installed" % a["name"], a["brew"]) if is_mac()
                 else ("%s is not on PATH" % a["bin"], a["pkg"]))
    print("breadcrumbs: %s. Install: %s, or see %s" % (what, how, a["page"]), file=err)
    return 2


def failed(app, why, err):
    print("breadcrumbs: %s did not start: %s" % (APPS[app]["name"], why), file=err)
    return 1


def open_window(app, home, show, out, err):
    argv = window_argv(app, home)
    if show:
        print(shlex.join(argv), file=out)
        return 0
    if not term.find_chrome():  # `term` needs it; without this check the new window would close at once
        print("breadcrumbs: term needs Chrome or Chromium. Install one (https://www.google.com/chrome/), "
              "or set BREADCRUMBS_CHROME to its program.", file=err)
        return 2
    try:
        if is_mac():  # `open` returns when LaunchServices has the app; it says so on stderr when it has not
            p = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=30)
            if p.returncode:
                text = (p.stderr or "").strip()
                if "Unable to find application" in text:
                    return missing(app, err)
                return failed(app, (text.splitlines() or ["open exit %d" % p.returncode])[-1], err)
        else:  # the app stays in the foreground while its window is up: do not wait for it
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                code = p.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                code = 0  # still running: the window is up
            if code:
                return failed(app, "it exited with status %d at once; --print shows its command line" % code, err)
    except FileNotFoundError:
        return missing(app, err)
    except (OSError, subprocess.SubprocessError) as e:
        return failed(app, e, err)
    print("breadcrumbs: opened a new %s window that runs term (Ctrl-C in it stops term)" % APPS[app]["name"], file=out)
    return 0


def answers(port, home):
    """True when the server on 127.0.0.1:port takes this home's token, so it serves this home. A home with no
    view.token has had no server; the file is not made here."""
    try:
        with open(os.path.join(home, "view.token"), encoding="utf-8") as f:
            key = f.read().strip()
        with urllib.request.urlopen("http://127.0.0.1:%d/api/trail?k=%s" % (port, key), timeout=2) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:  # the server of another home says 403
        e.close()
        return False
    except (OSError, ValueError, http.client.HTTPException):  # nothing there, or no HTTP
        return False


def open_web(home, port, show, out, err):
    wanted = PORT if port is None else port
    if show:
        print("webbrowser.open http://127.0.0.1:%d/  (the default browser; the token is added after the address "
              "and is not shown; a server for this home starts on the port if none answers)" % wanted, file=out)
        return 0
    server = None
    try:
        if not answers(wanted, home):
            try:
                server = view.make_server(home, port=wanted)
            except OSError as e:
                if e.errno != errno.EADDRINUSE or port is not None:
                    raise
                server = view.make_server(home, port=0)  # the default port is taken: any free port
        key = view.token(home)
    except FileNotFoundError:
        print("breadcrumbs: no store at %s" % home, file=err)
        return 1
    except OSError as e:
        print("breadcrumbs: cannot serve on port %d: %s" % (wanted, e.strerror or e), file=err)
        return 1
    url = "http://127.0.0.1:%d/" % (server.server_address[1] if server else wanted)
    try:
        if not webbrowser.open(url + "#k=" + key):
            print("breadcrumbs: no web browser started. `serve` prints an address that has the token.", file=err)
            return 1
        print("breadcrumbs explorer (read-only): %s  %s" % (url, "(Ctrl-C stops the server)" if server
                                                           else "(a server for this home was already running)"),
              file=out, flush=True)
        if server:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        if server:
            server.server_close()
    return 0


def run(where, home, port=None, show=False, out=None, err=None):
    """The `open` command. 0: done. 2: an app or Chrome is not there. 1: another failure."""
    out, err = out or sys.stdout, err or sys.stderr
    home = os.path.abspath(os.path.expanduser(home))
    if where in APPS:
        return open_window(where, home, show, out, err)
    return open_web(home, port, show, out, err)
