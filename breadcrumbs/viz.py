"""`viz`: open the explorer where the user is. One word, no options to know.

In cmux the page opens as a browser split beside the terminal (cmux browser open). In Kitty or Ghostty
it draws in the terminal (`term`). Anywhere else it opens the default browser. The page comes from one
read-only `serve` process on a fixed port; a second `viz` reuses it.

autostart: a Hermes chat that starts in a terminal gets the explorer once (cmux or tmux: a split; a bare
console: the OS opens it). See island.Tracker.on_session_start.
"""
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

from . import kitty

PORT = 8765


def listening(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def ensure_server(home, port=PORT):
    """The URL of the explorer of this home. A server that takes this home's token is used again (launch.answers).
    Else a detached `serve` starts: on the port, or on a free port when another program (the server of another
    home, for example) holds the port. The child gets this process's module path, so it has the same packages."""
    from .launch import answers
    from .view import token
    if not answers(port, home):
        if listening(port):
            port = free_port()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        subprocess.Popen([sys.executable, "-W", "ignore", "-m", "breadcrumbs", "--home", home, "serve",
                          "--port", str(port)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        for _ in range(100):
            if listening(port):
                break
            time.sleep(0.1)
    return "http://127.0.0.1:%d/#k=%s" % (port, token(home))


def split_kind(env=os.environ):
    """The terminal multiplexer that can split a window for us: "cmux", "tmux", or None (a bare console, or a
    terminal with no split command, such as Ghostty or Kitty alone)."""
    if env.get("CMUX_SURFACE_ID") or env.get("CMUX_WORKSPACE_ID"):
        return "cmux"
    if env.get("TMUX"):
        return "tmux"
    return None


def can_launch(env=os.environ, platform=sys.platform):
    """True when the OS can open a page: macOS and Windows always; Linux only with a display (else Python's
    webbrowser would start a text browser on the chat's own terminal)."""
    return platform in ("darwin", "win32") or bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def autostart(home, env=os.environ, err=None):
    """Hermes starts in a terminal: in cmux or tmux, split a window for the explorer (a cmux browser split, or a
    tmux pane that runs `term`); in a bare console, ask the OS to open it. Returns "cmux", "tmux", "os" or None.
    BREADCRUMBS_VIZ=0 turns it off."""
    err = err or sys.stderr
    if env.get("BREADCRUMBS_VIZ", "").lower() in ("0", "off", "no", "false"):
        return None
    kind = split_kind(env)
    if kind == "tmux" and shutil.which("tmux"):
        from .launch import term_argv
        argv = ["tmux", "split-window", "-h", "-d"] + (["-t", env["TMUX_PANE"]] if env.get("TMUX_PANE") else [])
        r = subprocess.run(argv + [shlex.join(term_argv(home))], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return "tmux"
        print("breadcrumbs viz: tmux could not split: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    url = ensure_server(home)
    if kind == "cmux" and shutil.which("cmux"):
        r = subprocess.run(["cmux", "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return "cmux"
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    if can_launch(env) and webbrowser.open(url):
        return "os"
    return None


def run(home, port=PORT, err=None, terminal=True):
    """terminal=False: the caller is not the user's shell (the /viz command of a chat), so `term` is not a choice."""
    err = err or sys.stderr
    url = ensure_server(home, port)
    cmux = shutil.which("cmux")
    if cmux:
        r = subprocess.run([cmux, "browser", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if r.returncode == 0:
            print("breadcrumbs viz: opened in a cmux split", file=err)
            return 0
        print("breadcrumbs viz: cmux could not open it: %s" % r.stderr.decode("utf-8", "replace").strip(), file=err)
    if terminal and sys.stdout.isatty() and kitty.supported():
        from . import term
        return term.run(url)
    webbrowser.open(url)
    print("breadcrumbs viz: opened in the browser: " + url, file=err)
    return 0
