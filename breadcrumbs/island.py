"""Islands: one codebase for each Hermes profile, the automatic offer, the handoff (/island) and the checks.

Hermes's profiles are independent islands: each one has its own memory, sessions and skills. When the
chats of one profile work in several codebases, its memory mixes them, and recall can bring back the wrong
one. breadcrumbs keeps each profile's state to one codebase in four parts:

- The automatic offer (two hooks): post_tool_call finds the codebase of each absolute path that a tool call
  names (the repository above it; a worktree or a submodule counts as its main repository). When a chat
  makes MIN_CALLS calls in a codebase that is not the island of its profile (a profile with no island, such
  as the default profile, gets the offer at the first call in its first codebase: FIRST_CALLS, or at its first
  reply for the launch folder's codebase when the chat has made no call yet), transform_llm_output adds a red offer to
  the end of the chat's next reply: the command that gives that codebase its own profile, or the profile
  that already has it. The offer comes once for each codebase, and it changes nothing by itself. The counts are a file
  (<home>/breadcrumbs/islands.json, the newest KEEP chats): a dev reload or a gateway restart keeps them.
- The handoff:

    /island [PATH [NAME]]                in a chat (the CLI, the desktop app, the gateway)
    hermes islands island PATH [NAME]    in a shell, where Hermes has that command (see "The shell form")

  gives the codebase at PATH (with no PATH: the folder that Hermes was launched in; a relative PATH is in that folder) its own profile, whose terminal.cwd is the repository root, and answers with
  the command to switch to it: `hermes -p NAME`.
- The visualizer: a chat that starts inside the island of its profile gets one NOTE at its first reply (the ways to
  see the profile's memory), and /viz opens the explorer of that memory from the chat (open_viz). A chat whose
  profile or session is tagged no-vis (novis.py) gets no NOTE and no explorer, at start or by /viz.
- The checks: /island status (`hermes islands status`) shows the island of this profile, the islands
  of all profiles, the problems of each island (a terminal.cwd that does not exist, a codebase that two
  profiles hold) and the codebases of this profile's own tool calls (its state.db, read-only).
  `/island --all` (`hermes islands status --all`) gives each profile's island and the codebases of its tool
  calls, with a verdict. `python -m breadcrumbs sources PROFILE` lists the codebases of one profile.

A new profile is a clone of the profile that runs the command (`hermes profile create NAME --clone-from
CURRENT --no-alias`: its config, keys, skills, plugins and the two memory files, not its sessions), then
`hermes -p NAME config set terminal.cwd ROOT`. Hermes's own CLI does both steps. A Hermes that carries the
folder of the active memory provider into a clone (hermes_cli/profile_memory_config.py) also copies
<home>/breadcrumbs, the store, when the memory.provider of the profile is breadcrumbs: the island then
starts with a copy of the crumbs of every codebase of that profile.
When the profile for the name exists without an island, it gets the island. When the target profile does not select
breadcrumbs (memory.provider), the answer also offers breadcrumbs, with the commands that select it.

The shell form: register() gives Hermes the CLI command `islands` where the plugin context has
register_cli_command. Hermes's loader of memory providers ignores that call, so a Hermes that loads
breadcrumbs as its memory provider has /island and no `hermes islands` (tests/hermes_integration.py
prints the result of `hermes islands status`).

Paths: islands place an absolute path, or a path under the home folder (~/x). On Windows an absolute path
starts with a drive (C:\\x or C:/x) or a host (\\\\host\\share), and ~\\x is also under the home folder. A
message shows a path under the home folder as ~/x on every platform, and /island reads that form back.
"""
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import ace, novis
from .ace import setting  # one reading of config.yaml for the whole plugin (a column-0 '#' stays in its block)

HOME = os.path.expanduser("~")
CATALOG_ENTRY = "breadcrumbs"  # the catalog entry: `hermes plugins install breadcrumbs` takes the pinned commit, a path takes HEAD
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")  # hermes_constants.PROFILE_ID_RE
USAGE = "/island [PATH [NAME]]"
# The chats of a local session. "local" is the gateway's own transport: gatewayd runs the CLI, TUI and desktop chats,
# and a plugin command there gets platform "local" (Platform.LOCAL). Any other platform is a messaging chat.
LOCAL_SURFACES = ("cli", "tui", "desktop", "acp", "local")
TERMINAL_SURFACES = ("cli", "tui")  # the chats that run in a terminal window
_viz_started = False  # the explorer opens once per process, at the first terminal chat
GATEWAY_DISCLAIMER = ("`/island` is not for messaging chats (%s). It changes profiles on the machine that runs Hermes. "
                      "Use it in a local session (the CLI, the TUI or the desktop app), or run `hermes islands island PATH` in a shell.")
MIN_CALLS = 3  # tool calls in a codebase before a chat counts it, in a profile that has an island
FIRST_CALLS = 1  # the same for a profile with no island: its first codebase gets the offer at the first call
KEEP = 64  # chats kept in <home>/breadcrumbs/islands.json (the oldest go first)
STATE = "islands.json"
SCAN_ROWS = 20000  # the newest messages with tool calls that a scan of a state.db reads
PATH_KEYS = {"path", "file_path", "workdir", "cwd", "dir", "directory", "file", "paths", "files"}
PATH_RE = re.compile(r"(?<![\w.~/:-])(~?/[\w.@+-]+(?:/[\w.@+-]+)*)")
# Windows: a drive or ~, then names with \ or / between them (code and JSON text double a backslash).
WIN_PATH_RE = re.compile(r"(?<![\w.~/\\:-])((?:[A-Za-z]:|~)[\\/]+[\w.@+-]+(?:[\\/]+[\w.@+-]+)*)")
WINDOWS = os.name == "nt"  # the text forms of a path: is_path, full_path, paths_in, words
WHY = "Why: each profile is an island with its own memory, so recall stays on one codebase and stays exact."
COMMAND_HELP = ("Give the codebase you launched Hermes in (or the one at <path>) its own profile (one codebase per "
                "profile); status: the islands and their checks; --all: every profile")

# What ACE does (README "Keep the context at a steady size: ACE"). It names no multiplier: the bench is not run again.
TURBO = (
    "Turbo on token effectiveness: breadcrumbs (ACE). A long chat folds into a layer of crumbs only when a request "
    "passes the target, and no model call writes the fold. Between folds the layer does not change, so the prompt "
    "cache holds. Every folded turn is one `crumb(id, level)` call away. At a target of 8k or 16k tokens a plain "
    "window of the newest messages holds more exact text than the layer."
)


def hermes_root() -> str:
    """The Hermes root (~/.hermes, or a custom root): its profiles are under <root>/profiles."""
    try:
        from hermes_constants import get_default_hermes_root

        return str(get_default_hermes_root())
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".hermes")


def current_home() -> str:
    """The home of the profile that runs the command or the hook. A gateway that serves several profiles
    keeps it in a context variable (hermes_constants.get_hermes_home), not in os.environ."""
    return ace._home()


# -- codebases -------------------------------------------------------------------------------------------
def is_path(text: str) -> bool:
    """True for a path that islands can place: an absolute path, or a path under the home folder (~/x). On
    Windows an absolute path starts with a drive (C:\\x, C:/x) or a host (\\\\host\\share), and ~\\x is
    also under the home folder. Other text is not a path here: "." is the folder of a process."""
    if WINDOWS:
        return text.startswith(("~/", "~\\", "\\\\")) or bool(re.match(r"[A-Za-z]:[\\/]", text))
    return text.startswith(("/", "~/"))


def full_path(path: str, home: Optional[str] = None) -> str:
    """The absolute form of a path; ~/x (on Windows also ~\\x) is under `home`."""
    if path.startswith("~/") or (WINDOWS and path.startswith("~\\")):
        return os.path.abspath((home or HOME) + path[1:])
    return os.path.abspath(os.path.expanduser(path))


def _repo(d: str) -> Optional[str]:
    """The repository that folder d holds, or None. A .git file (a linked worktree, a submodule) names a
    folder in the main repository's .git: the codebase is that main repository."""
    git = os.path.join(d, ".git")
    if os.path.isdir(git):
        return d
    try:
        with open(git, encoding="utf-8") as f:
            m = re.match(r"gitdir:\s*(.+?)\s*$", f.readline())
    except (OSError, UnicodeDecodeError):
        return None
    if not m:
        return None
    main, sep, _ = os.path.normpath(os.path.join(d, m.group(1))).partition(os.sep + ".git" + os.sep)
    return main if sep else d


SOURCE_MARKERS = ("pyproject.toml", "setup.py", "package.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
                  "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "CMakeLists.txt", "Makefile", "Gemfile",
                  "composer.json", "mix.exs", "Package.swift")  # a source tree with no git repository


def root_of(path: str, home: Optional[str] = None, hermes: Optional[str] = None) -> Optional[str]:
    """The codebase of an absolute path (the file need not exist): a repository under the home folder, else a
    source tree (a folder with a project file: SOURCE_MARKERS). None outside both, for Hermes's own files (the platform is not the chat's codebase), and for tools: a
    repository outside the home folder (/opt/homebrew) or in a hidden folder (~/.pyenv, ~/.nvm)."""
    home = home or HOME
    hermes = hermes or hermes_root()
    p = full_path(path, home)
    if p == hermes or p.startswith(hermes + os.sep):
        return None
    while p != os.path.dirname(p) and not os.path.isdir(p):
        p = os.path.dirname(p)
    start = p
    for found in (_repo, lambda d: d if any(os.path.exists(os.path.join(d, m)) for m in SOURCE_MARKERS) else None):
        p = start  # a git repository first (the nearest), else the nearest source tree
        while p.startswith(home + os.sep):
            r = found(p)
            if r:
                rel = os.path.relpath(r, home).split(os.sep)
                return None if any(part.startswith(".") for part in rel) else r  # ".." too: outside home
            p = os.path.dirname(p)
    return None


def short(path: str, home: Optional[str] = None) -> str:
    """A path for a message: ~/x for a path under the home folder, with / on every platform. So a message
    is the same text on Windows, and /island reads the path back."""
    home = home or HOME
    return "~" + path[len(home):].replace(os.sep, "/") if path.startswith(home + os.sep) else path


def paths_in(args) -> List[str]:
    """The paths that a tool call names (see is_path): its path arguments, and the paths in a command or
    code."""
    out: List[str] = []
    in_text = WIN_PATH_RE if WINDOWS else PATH_RE
    for k, v in (args if isinstance(args, dict) else {}).items():
        if k in PATH_KEYS:
            out += [v] if isinstance(v, str) else [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
        elif k in ("command", "code", "cmd") and isinstance(v, str):
            out += in_text.findall(v)
    return [p for p in out if is_path(p)]


def roots_of(args, home: Optional[str] = None, hermes: Optional[str] = None) -> Set[str]:
    """The codebases of the paths that a tool call names."""
    paths = paths_in(args)
    if not paths:
        return set()
    hermes = hermes or hermes_root()
    return {r for r in (root_of(p, home, hermes) for p in paths) if r}


# -- profiles --------------------------------------------------------------------------------------------
def profile_home(name: str, hermes: Optional[str] = None) -> str:
    hermes = hermes or hermes_root()
    return hermes if name == "default" else os.path.join(hermes, "profiles", name)


def profile_of(home_dir: str, hermes: Optional[str] = None) -> str:
    hermes = hermes or hermes_root()
    home_dir = os.path.abspath(home_dir)
    return os.path.basename(home_dir) if os.path.dirname(home_dir) == os.path.join(hermes, "profiles") else "default"


def own_root(home_dir: str, home: Optional[str] = None, hermes: Optional[str] = None) -> Optional[str]:
    """The island of a profile: the codebase that holds its terminal.cwd."""
    cwd = setting(home_dir, "terminal", "cwd")
    return root_of(cwd, home, hermes) if cwd and is_path(cwd) else None


def profiles(hermes: Optional[str] = None) -> List[Tuple[str, str]]:
    """(name, home) of the default profile, then of each folder in <root>/profiles, by name."""
    hermes = hermes or hermes_root()
    out = [("default", hermes)]
    base = os.path.join(hermes, "profiles")
    try:
        out += [(n, os.path.join(base, n)) for n in sorted(os.listdir(base))
                if not n.startswith(".") and os.path.isdir(os.path.join(base, n))]
    except OSError:
        pass
    return out


def islands(home: Optional[str] = None, hermes: Optional[str] = None) -> dict:
    """{codebase: profile} for every profile that has one (the first profile by name wins)."""
    hermes = hermes or hermes_root()
    out = {}
    for name, home_dir in profiles(hermes):
        r = own_root(home_dir, home, hermes)
        if r:
            out.setdefault(r, name)
    return out


def is_profile(name: str, hermes: Optional[str] = None) -> bool:
    """True when the profile's folder exists (Hermes refuses to create a profile over any folder)."""
    return name == "default" or os.path.isdir(profile_home(name, hermes))


def plugin_folder(home_dir: str, plugin: str) -> Optional[str]:
    """The folder in <home_dir>/plugins whose plugin.yaml has `name: <plugin>`, or None."""
    base = os.path.join(home_dir, "plugins")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    pattern = re.compile(r"^name:\s*['\"]?%s['\"]?\s*$" % re.escape(plugin), re.M)
    for name in names:
        try:
            with open(os.path.join(base, name, "plugin.yaml"), encoding="utf-8") as f:
                if pattern.search(f.read()):
                    return name
        except (OSError, UnicodeDecodeError):
            continue
    return None


def valid_name(name: str) -> Optional[str]:
    """None when name can be a new profile; else the reason (Hermes's own check when Hermes is here)."""
    if name == "default":
        return "`default` is Hermes's own profile, not an island"
    try:
        from hermes_cli.profiles import validate_profile_name

        validate_profile_name(name)
    except ImportError:
        if not PROFILE_ID.match(name):
            return "a profile name is 1 to 64 of a-z, 0-9, - and _, and starts with a letter or a digit"
    except ValueError as e:
        return str(e)
    return None


def profile_name(root: str) -> str:
    """The profile name of a codebase: its folder name in Hermes's profile form (a reserved name gets
    "-island")."""
    base = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(root.rstrip(os.sep)).lower()).strip("-_")[:64].strip("-_")
    name = base or "island"
    if valid_name(name):
        name = (name[:57] + "-island").strip("-_")
    return name


# -- the offer of breadcrumbs ----------------------------------------------------------------------------
def offer(name: str, hermes: Optional[str] = None) -> List[str]:
    """The lines that offer breadcrumbs to profile `name`; none when its memory.provider is breadcrumbs."""
    home_dir = profile_home(name, hermes)
    if setting(home_dir, "memory", "provider") == "breadcrumbs":
        return []
    lines = [TURBO]
    if not plugin_folder(home_dir, "breadcrumbs"):
        lines.append("  hermes -p %s plugins install %s --enable" % (name, CATALOG_ENTRY))
    lines += ["  hermes -p %s config set memory.provider breadcrumbs" % name,
              "  hermes -p %s config set context.engine breadcrumbs" % name]
    return lines


# -- the handoff -----------------------------------------------------------------------------------------
Runner = Callable[[List[str], str], Tuple[int, str]]


def hermes_argv() -> List[str]:
    """Hermes's own CLI: its entry point (Hermes's resolve_hermes_bin), else `hermes` on PATH or in
    ~/.local/bin, else this Python with -m hermes_cli.main."""
    try:
        from hermes_cli.relaunch import resolve_hermes_bin

        found = resolve_hermes_bin()
        if found:
            return [found]
    except Exception:
        pass
    for path in (shutil.which("hermes"), os.path.join(HOME, ".local", "bin", "hermes")):
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return [path]
    return [sys.executable, "-m", "hermes_cli.main"]


CHILD_DROPS = ("HERMES_YOLO_MODE", "HERMES_NONINTERACTIVE")  # the child never inherits a switch that skips approval or asks nothing


def run_hermes(args: List[str], home_dir: str) -> Tuple[int, str]:
    """Run `hermes ARGS` with HERMES_HOME = home_dir; (exit code, output). The child's environment is ours without
    CHILD_DROPS."""
    env = {k: v for k, v in os.environ.items() if k not in CHILD_DROPS}
    env["HERMES_HOME"] = home_dir
    try:
        done = subprocess.run(hermes_argv() + args, env=env, capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


@dataclass
class Result:
    ok: bool  # the codebase has an island now (or the status was shown)
    lines: List[str] = field(default_factory=list)
    profile: Optional[str] = None
    calls: List[List[str]] = field(default_factory=list)  # the hermes commands that ran

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _tail(output: str, n: int = 6) -> List[str]:
    return ["  " + line for line in output.strip().splitlines()[-n:]]


def _s(n: int) -> str:
    return "" if n == 1 else "s"


# -- the checks ------------------------------------------------------------------------------------------
def checks(home: Optional[str] = None, hermes: Optional[str] = None) -> List[str]:
    """The problems of the islands of all profiles: a terminal.cwd that does not exist, and a codebase that
    two profiles hold (their memories mix it; the first profile by name keeps the island)."""
    home = home or HOME
    hermes = hermes or hermes_root()
    lines: List[str] = []
    first: Dict[str, str] = {}
    for name, home_dir in profiles(hermes):
        cwd = setting(home_dir, "terminal", "cwd")
        if not cwd or not is_path(cwd):
            continue
        full = full_path(cwd, home)
        if not os.path.exists(full):
            lines.append("`%s`: its terminal.cwd `%s` does not exist" % (name, cwd))
            continue
        r = root_of(full, home, hermes)
        if r and r in first:
            lines.append("`%s`: its terminal.cwd is in `%s`, the island of `%s` (two profiles, one codebase)"
                         % (name, short(r, home), first[r]))
        elif r:
            first[r] = name
    return lines


def _check_lines(home: Optional[str], hermes: Optional[str]) -> List[str]:
    problems = checks(home, hermes)
    if not problems:
        return ["Checks: every island is sound."]
    return ["Checks: %d problem%s:" % (len(problems), _s(len(problems)))] + ["  " + p for p in problems]


def scan(home_dir: str, home: Optional[str] = None, hermes: Optional[str] = None,
         rows: int = SCAN_ROWS) -> Dict[str, List[int]]:
    """{codebase: [calls, chats, last time]} from the newest `rows` messages with tool calls in
    <home_dir>/state.db, Hermes's session store (opened read-only)."""
    from urllib.parse import quote

    path = os.path.join(home_dir, "state.db")
    if not os.path.isfile(path):
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % quote(path), uri=True, timeout=5)
        try:
            found = con.execute("SELECT session_id, timestamp, tool_calls FROM messages WHERE tool_calls IS NOT NULL"
                                " AND tool_calls != '' ORDER BY id DESC LIMIT ?", (rows,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out: Dict[str, List[int]] = {}
    seen: Set[Tuple[str, str]] = set()
    known: Dict[str, Optional[str]] = {}
    hermes = hermes or hermes_root()
    for sid, ts, calls in found:
        try:
            calls = json.loads(calls)
        except (TypeError, ValueError):
            continue
        for c in calls if isinstance(calls, list) else []:
            args = (c.get("function") or {}).get("arguments") if isinstance(c, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"command": args}
            roots = set()
            for p in paths_in(args):
                if p not in known:
                    known[p] = root_of(p, home, hermes)
                if known[p]:
                    roots.add(known[p])
            for r in roots:
                e = out.setdefault(r, [0, 0, 0])
                e[0] += 1
                e[1] += (sid, r) not in seen
                e[2] = max(e[2], int(ts or 0))
                seen.add((sid, r))
    return out


def _ranked(got: Dict[str, List[int]]) -> List[Tuple[str, List[int]]]:
    return sorted(got.items(), key=lambda kv: (-kv[1][0], kv[0]))


def status(current: str, home: Optional[str] = None, hermes: Optional[str] = None) -> Result:
    """The island of this profile, the islands of all profiles, their checks, the codebases of this
    profile's tool calls, and the offer of breadcrumbs."""
    hermes = hermes or hermes_root()
    mine = own_root(profile_home(current, hermes), home, hermes)
    all_islands = islands(home, hermes)
    lines = ["breadcrumbs islands: one codebase for each profile.",
             "This profile (`%s`): %s." % (current, "its island is `%s`" % short(mine, home) if mine
                                           else "no island (its terminal.cwd is in no codebase)")]
    if all_islands:
        lines.append("Islands:")
        lines += ["  %s -> %s" % (short(r, home), n) for r, n in sorted(all_islands.items(), key=lambda kv: kv[1])]
    lines += _check_lines(home, hermes)
    got = scan(profile_home(current, hermes), home, hermes)
    if got:
        lines.append("Codebases in this profile's newest tool calls:")
        ranked = _ranked(got)
        for r, (calls, chats, _) in ranked[:8]:
            owner = all_islands.get(r)
            note = ("the island of this profile" if r == mine else
                    "the island of `%s`: `hermes -p %s`" % (owner, owner) if owner else
                    "no island: `/island %s`" % short(r, home))
            lines.append("  %s: %d call%s in %d chat%s (%s)" % (short(r, home), calls, _s(calls), chats, _s(chats), note))
        if len(ranked) > 8:
            lines.append("  and %d more" % (len(ranked) - 8))
    else:
        lines.append("No codebase in this profile's tool calls.")
    lines.append("To give a codebase its own profile: `%s`. All profiles: `/island --all`." % USAGE)
    return Result(True, lines + offer(current, hermes), current)


def report(home: Optional[str] = None, hermes: Optional[str] = None) -> Result:
    """Each profile: its island, the codebases of its newest tool calls (shares of the calls), and a
    verdict: sound, mixes N codebases, or the /island command that gives it its codebase. Then the checks."""
    home = home or HOME
    hermes = hermes or hermes_root()
    owners = islands(home, hermes)
    lines = ["breadcrumbs islands of all profiles (the codebases of each profile's newest %d messages with tool "
             "calls):" % SCAN_ROWS]
    for name, home_dir in profiles(hermes):
        own = own_root(home_dir, home, hermes)
        where = "island `%s`" % short(own, home) if own else "no island"
        ranked = _ranked(scan(home_dir, home, hermes))
        if not ranked:
            lines.append("  %s: %s; no codebase in its calls" % (name, where))
            continue
        total = sum(v[0] for _, v in ranked)
        shares = ", ".join("%s %d%%" % (short(r, home), round(100.0 * v[0] / total)) for r, v in ranked[:4])
        shares += ", %d more" % (len(ranked) - 4) if len(ranked) > 4 else ""
        top, owner = ranked[0][0], owners.get(ranked[0][0])
        if own:
            verdict = ("sound" if [r for r, _ in ranked] == [own] else
                       "mixes %d codebases" % len(ranked) if len(ranked) > 1 else "its calls are outside its island")
        else:
            bind = ("`%s` is the island of `%s`" % (short(top, home), owner) if owner else
                    "" if name == "default" else "`/island %s %s`" % (short(top, home), name))
            verdict = "; ".join(v for v in ["mixes %d codebases" % len(ranked) if len(ranked) > 1 else "",
                                             ("the most calls: " if len(ranked) > 1 and bind else "") + bind] if v)
            verdict = verdict or "one codebase"
        lines.append("  %s: %s; its calls: %s: %s" % (name, where, shares, verdict))
    return Result(True, lines + _check_lines(home, hermes))


def handoff(path: str, name: Optional[str] = None, *, current: Optional[str] = None, run: Optional[Runner] = None,
            home: Optional[str] = None, hermes: Optional[str] = None) -> Result:
    """Give the codebase at `path` its own profile (see the module text). `current` is the profile that
    runs the command (the clone source); `run` runs Hermes's CLI (a test gives its own)."""
    home = home or HOME
    hermes = hermes or hermes_root()
    current = current or profile_of(current_home(), hermes)
    run = run or run_hermes
    p = full_path(path, home)
    if not os.path.exists(p):
        return Result(False, ["No file or folder at `%s`." % path])
    root = root_of(p, home, hermes)
    if not root:
        return Result(False, ["`%s` is in no codebase. A codebase is a git repository or a source tree under your home folder, "
                              "not in a hidden folder and not in Hermes's own folder. Run `/island PATH` with a "
                              "codebase, or start Hermes inside one." % short(p, home)])
    where = short(root, home)
    name = name.strip().lower() if name else None  # Hermes's form: `-p Work` selects work
    owner = islands(home, hermes).get(root)
    if owner and (not name or name == owner):
        if owner == current:
            return Result(True, ["`%s` is the island of this profile (`%s`)." % (where, owner)] + offer(owner, hermes), owner)
        return Result(True, ["`%s` has its own profile: `%s`. Continue that work there: `hermes -p %s`" % (where, owner, owner)]
                      + offer(owner, hermes), owner)
    if owner:
        return Result(False, ["`%s` is the island of `%s`, not of `%s`; /island does not move an island. "
                              "Continue that work there: `hermes -p %s`" % (where, owner, name, owner)], owner)
    target = name or profile_name(root)
    bad = valid_name(target)
    if bad:
        return Result(False, ["Profile name `%s`: %s." % (target, bad)])
    calls: List[List[str]] = []
    created = False
    if is_profile(target, hermes):
        theirs = own_root(profile_home(target, hermes), home, hermes)
        if theirs and islands(home, hermes).get(theirs) == target:  # a profile that shares an island has none
            return Result(False, ["Profile `%s` is the island of `%s`. Give another name: `/island %s NAME`"
                                  % (target, short(theirs, home), where)])
        # the profile for the name exists and has no island: it gets this one (its terminal.cwd, below)
    else:
        candidate = "src-" + target
        if not name and is_profile(candidate, hermes) and not own_root(profile_home(candidate, hermes), home, hermes):
            target = candidate  # a profile for this codebase exists and has no island: it gets this one
        else:
            calls.append(["profile", "create", target, "--clone-from", current, "--no-alias", "--description",
                          "The island of %s (one codebase): made by /island (breadcrumbs)." % where])
            code, out = run(calls[-1], profile_home(current, hermes))
            if code != 0:
                return Result(False, ["`hermes profile create %s` failed (exit %d):" % (target, code)] + _tail(out),
                              None, calls)
            created = True
    calls.append(["-p", target, "config", "set", "terminal.cwd", root])
    code, out = run(calls[-1], profile_home(target, hermes))
    if code != 0:
        return Result(False, ["`hermes -p %s config set terminal.cwd` failed (exit %d):" % (target, code)] + _tail(out),
                      target, calls)
    now = own_root(profile_home(target, hermes), home, hermes)
    if now != root:
        return Result(False, ["Profile `%s`: its terminal.cwd reads `%s`, not a folder of `%s`."
                              % (target, setting(profile_home(target, hermes), "terminal", "cwd"), where)], target, calls)
    if created:
        head = ("`%s` has its own profile now: `%s` (terminal.cwd = %s). It is a clone of `%s`: its config, keys, "
                "skills, plugins and memory files." % (where, target, root, current))
    else:
        head = "`%s` is now the island of profile `%s` (terminal.cwd = %s)." % (where, target, root)
    return Result(True, [head, "Continue that work there: `hermes -p %s`" % target] + offer(target, hermes), target, calls)


# -- the automatic offer (the hooks) ---------------------------------------------------------------------
NUMBERS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _calls(n: int) -> str:
    return "%d call%s" % (n, "" if n == 1 else "s")


def _and(items: List[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def chat_folder() -> str:
    """The folder of the chat that the running hook or slash handler serves: its most local folder, the root of
    Hermes's own path Tab completion for the chat. Hooks and slash handlers run in the gateway process (gatewayd
    also runs the CLI and TUI chats), whose own folder is the install root, so os.getcwd() and TERMINAL_CWD say
    nothing about the chat. The gateway keeps the folder that the CLI or the TUI sent from its shell at session
    start (the session's frozen cwd: complete.path lists a path word there), pins it for each turn and each slash
    command, and agent.runtime_cwd.resolve_agent_cwd() (a public plugin API) reads it, on every surface: CLI, TUI,
    Desktop, ACP and the messaging platforms. "" outside Hermes."""
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        return str(resolve_agent_cwd())
    except Exception:  # not inside Hermes, or an older Hermes
        return ""


def launch_folder(home: Optional[str] = None, hermes: Optional[str] = None) -> str:
    """The folder that the chat started in: the folder Hermes pinned for it (chat_folder). Outside Hermes (the
    `hermes islands` command, a test): TERMINAL_CWD, else the process folder; the first that is in a codebase wins."""
    cwd = chat_folder()
    if cwd:
        return cwd
    cwds = [c for c in (os.environ.get("TERMINAL_CWD"), os.getcwd()) if c and is_path(c)]
    return next((c for c in cwds if root_of(c, home, hermes)), cwds[0] if cwds else "")


def launch_root(home: Optional[str] = None, hermes: Optional[str] = None) -> Optional[str]:
    """The codebase of the folder that the chat started in, or None."""
    cwd = launch_folder(home, hermes)
    return root_of(cwd, home, hermes) if cwd else None


def chat_root(profile_dir: str, session_id: str) -> Optional[str]:
    """The codebase with the most tool calls in the chat (islands.json), or None."""
    roots = (load(profile_dir).get(session_id) or {}).get("roots") or {}
    return max(sorted(roots), key=lambda r: roots[r]) if roots else None


def recent_root(profile_dir: str) -> Optional[str]:
    """The codebase with the most calls in the newest chat of the profile (islands.json), or None."""
    chats = [c for c in load(profile_dir).values() if isinstance(c, dict) and c.get("roots")]
    if not chats:
        return None
    roots = max(chats, key=lambda c: c.get("t", 0))["roots"]
    return max(sorted(roots), key=lambda r: roots[r])


def state_path(profile_dir: str) -> str:
    return os.path.join(profile_dir, "breadcrumbs", STATE)


def load(profile_dir: str) -> dict:
    """{session id: {"roots": {codebase: calls}, "warned": [codebase], "t": time}} of a profile."""
    try:
        with open(state_path(profile_dir), encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save(profile_dir: str, state: dict) -> None:
    """Write the newest KEEP chats (atomic replace: a reader never sees half a file)."""
    if len(state) > KEEP:
        for sid in sorted(state, key=lambda s: state[s].get("t", 0))[:len(state) - KEEP]:
            del state[sid]
    path = state_path(profile_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, separators=(",", ":"))
    os.replace(tmp, path)


class Tracker:
    """The codebases of each chat of a profile, and the offer that the chat's next reply gets when it
    reaches a codebase that is not its own. The counts are a file (<profile>/breadcrumbs/islands.json): the
    dev shim purges breadcrumbs' modules at each agent build, and a gateway restart starts a new process.
    With no profile_home, the profile is the one that runs the hook (a gateway serves several)."""

    def __init__(self, home: Optional[str] = None, hermes: Optional[str] = None, profile_home: Optional[str] = None):
        self.home, self.hermes, self.profile_home = home, hermes, profile_home
        self.lock = threading.Lock()

    def _profile(self) -> str:
        return os.path.abspath(self.profile_home or current_home())

    def post_tool_call(self, tool_name: str = "", args=None, session_id: str = "", **_) -> None:
        roots = roots_of(args, self.home, self.hermes)
        if not roots:
            return None
        profile, sid = self._profile(), session_id or "-"
        with self.lock:
            state = load(profile)
            chat = state.setdefault(sid, {})
            counts = chat.setdefault("roots", {})
            for r in sorted(roots):
                counts[r] = counts.get(r, 0) + 1
            chat["t"] = int(time.time())
            save(profile, state)
        return None

    def on_session_start(self, session_id: str = "", platform: str = "", **_) -> None:
        """The startup hook: Hermes calls it once, before the first reply of a chat. It probes the folder that the
        chat starts in and keeps its codebase (the chat's "launch"), so the first reply can already warn. A chat
        that starts in a terminal (the CLI or the TUI) also opens the explorer once per process (viz.autostart), unless
        its profile or the session is tagged no-vis."""
        if platform in TERMINAL_SURFACES and not novis.blocked(self._profile(), session_id, self.hermes):
            start_viz()
        sid = session_id or "-"
        launch = launch_root(self.home, self.hermes)
        if not launch:
            return None
        profile = self._profile()
        with self.lock:
            state = load(profile)
            chat = state.setdefault(sid, {})
            chat["launch"] = launch
            chat["t"] = int(time.time())
            save(profile, state)
        return None

    def transform_llm_output(self, response_text=None, session_id: str = "", **_) -> Optional[str]:
        """The CAUTION routine. The codebases of a chat are its launch codebase (0 calls at the start) and the
        codebases of its tool calls. A codebase is active when it is the launch codebase, or has the calls (MIN_CALLS
        in a profile that has an island, FIRST_CALLS in one that has none). Each active codebase that is not the
        island of the profile gets one warning, appended to the reply."""
        if not isinstance(response_text, str) or not response_text:
            return None
        profile, sid = self._profile(), session_id or "-"
        with self.lock:
            state = load(profile)
            chat = state.get(sid) if isinstance(state.get(sid), dict) else {}
            own = own_root(profile, self.home, self.hermes)
            launch = chat.get("launch") or launch_root(self.home, self.hermes)  # no startup hook: probe now
            calls = dict(chat.get("roots") or {})
            seen = {launch: calls.get(launch, 0)} if launch else {}
            seen.update({r: n for r, n in calls.items() if r != launch})  # the launch codebase first, then by first use
            need = MIN_CALLS if own else FIRST_CALLS
            active = [r for r, n in seen.items() if r == launch or n >= need]
            warned = chat.get("warned") or []
            new = [r for r in active if r != own and r not in warned]
            hint = bool(own and launch == own and not chat.get("hinted"))  # launched inside the island: say how to see it
            hint = hint and not novis.blocked(profile, session_id, self.hermes)  # a no-vis chat gets no NOTE
            if not new and not hint:
                return None
            chat = dict(chat, warned=warned + new, t=int(time.time()))
            if hint:
                chat["hinted"] = True
            state[sid] = chat
            if launch:
                chat["launch"] = launch
            save(profile, state)
        base = own or active[0]
        parts = ([self.offer_text(profile, base, seen, launch, active, new, bool(own))] if new else []) \
            + ([self.visualizer_text(profile, own)] if hint else [])
        return response_text + "\n\n" + "\n\n".join(parts)  # appended: the stream stays

    def visualizer_text(self, profile: str, own: str) -> str:
        """The NOTE for a chat that starts inside the island of its profile: the means to open the visualizer of
        this profile's memory (the desktop app's map item, the explorer in a terminal or a browser)."""
        run = "PYTHONPATH=%s python3 -m breadcrumbs --home %s" % (
            short(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), self.home),
            short(os.path.join(profile, "breadcrumbs"), self.home))
        return ("> [!NOTE]\n> \U0001F535 **breadcrumbs: this chat starts in the island `%s` (profile `%s`). See its memory:**\n"
                "> - In this chat: `/viz` opens the explorer (no command line).\n"
                "> - Desktop app: the sidebar item that opens the map, graph, Gantt and Venn (also in the command palette).\n"
                "> - Kitty or Ghostty: `%s term`\n"
                "> - A browser: `%s serve --open`" % (short(own, self.home), profile_of(profile, self.hermes), run, run))

    def offer_text(self, profile: str, base: str, seen: Dict[str, int], launch: Optional[str], active: List[str],
                   new: List[str], own: bool) -> str:
        h = self.home
        owners = islands(self.home, self.hermes)
        me = profile_of(profile, self.hermes)

        def where(rs: List[str]) -> str:  # "starts in `x` (the launch folder) and works in `y` (3 calls)"
            starts = ["`%s` (the launch folder)" % short(r, h) for r in rs if r == launch and not seen[r]]
            works = ["`%s` (%s)" % (short(r, h), _calls(seen[r])) for r in rs if not (r == launch and not seen[r])]
            return " and ".join(p for p in ("starts in " + _and(starts) if starts else "", "works in " + _and(works) if works else "") if p)
        if own:
            head = "this profile is the island of `%s`, and this chat %s" % (short(base, h), where(new))
        elif len(active) == 1:
            head = "this profile (`%s`) has no island, and this chat %s" % (me, where(active))
        else:
            word = NUMBERS[len(active)] if len(active) < len(NUMBERS) else str(len(active))
            head = "this chat mixes %s codebases: %s" % (word, _and([
                "`%s` (%s)" % (short(r, h), "the launch folder" if r == launch and not seen[r] else _calls(seen[r]))
                for r in active]))
        acts = []
        for r in new:
            owner = owners.get(r)
            if owner and owner != me:
                acts.append("`%s` has its own profile: continue that work there: `hermes -p %s`." % (short(r, h), owner))
            else:
                acts.append("Run `/island %s` to give it its own profile." % short(r, h))
        return "> [!CAUTION]\n> \U0001F534 **breadcrumbs: %s.** %s\n> %s" % (head, " ".join(acts), WHY)


# -- /viz ------------------------------------------------------------------------------------------------
VIZ_HELP = "open the explorer of this profile's memory (the map, graph, Gantt and Venn; read-only)"
VIZ_DISCLAIMER = "`/viz` is not for messaging chats (%s). It opens a page on the machine that runs Hermes. Use it in a local session."


def start_viz(profile_dir: Optional[str] = None) -> bool:
    """The chat starts in a terminal: open the explorer once per process (viz.autostart: a split in cmux or tmux, else
    the OS opens it). It runs in a thread, because a first server start can take seconds, and never raises."""
    global _viz_started
    if _viz_started:
        return False
    _viz_started = True
    home = os.path.join(os.path.abspath(profile_dir or current_home()), "breadcrumbs")  # read here: the thread has no profile scope

    def work():
        try:
            from . import viz
            viz.autostart(home)
        except Exception:
            logging.getLogger(__name__).debug("breadcrumbs viz: autostart failed", exc_info=True)

    threading.Thread(target=work, name="breadcrumbs-viz", daemon=True).start()
    return True


def open_viz(profile_dir: Optional[str] = None, hermes: Optional[str] = None, session_id: str = "") -> str:
    """/viz: the explorer of the profile's store, opened on this machine (viz.run: a cmux split, else the
    default browser). The answer names the place and never the URL: the URL holds the view token. A chat whose profile
    or session is tagged no-vis opens nothing."""
    import io
    from . import viz
    profile_dir = os.path.abspath(profile_dir or current_home())
    why = novis.blocked(profile_dir, session_id, hermes)
    if why:
        return "breadcrumbs: the explorer did not open: %s." % why
    said = io.StringIO()
    try:
        viz.run(os.path.join(profile_dir, "breadcrumbs"), err=said, terminal=False)
    except Exception as e:  # a chat command answers; it never raises
        return "breadcrumbs: the explorer did not open: %s: %s" % (type(e).__name__, e)
    where = "a cmux split" if "cmux split" in said.getvalue() else "the browser"
    return "breadcrumbs: the explorer of profile `%s` is open in %s (read-only, on this machine)." % (
        profile_of(profile_dir, hermes), where)


# -- the entry points ------------------------------------------------------------------------------------
def words(raw: str) -> List[str]:
    """The words of the text after /island. Quotes keep a path with spaces in one word. On POSIX a
    backslash is the escape character of the shell. On Windows it is a part of a path, so it stays."""
    try:
        if not WINDOWS:
            return shlex.split(raw)
        return [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "'\"" else w for w in shlex.split(raw, posix=False)]
    except ValueError:  # no closing quote
        return raw.split()


def command(raw_args: str = "", session_id: str = "", ask: Optional[Callable[[str, list], Optional[str]]] = None,
            leave: Optional[Callable[[], bool]] = None) -> str:
    """/island [PATH [NAME]], /island status or /island --all: the slash command (the CLI chat, the desktop app, the gateway).
    The bare form asks first, in a modal picker (`ask` is ctx.ask_choice of Hermes): it changes profiles, so
    only a "yes" acts. A PATH is a deliberate answer already, so it does not ask. After a "yes" that made
    the island, `leave` asks Hermes to end this chat (a best effort: a plugin has no exit API)."""
    words_ = words(raw_args or "")
    current = profile_of(current_home())
    if words_ == ["status"]:
        return status(current).text
    if words_ == ["--all"]:
        return report().text
    if len(words_) > 2 or (words_ and words_[0].startswith("-")):
        return "Usage: `%s` (no PATH: the folder Hermes was launched in), `/island status` or `/island --all`" % USAGE
    if not words_ or words_ == ["."]:  # the launch folder is a deliberate placement ("." is its default argument)
        folder = launch_folder() or os.environ.get("TERMINAL_CWD") or os.getcwd()
        if not root_of(folder):  # launched in the home folder: the codebase of this chat's own tool calls
            folder = chat_root(current_home(), session_id) or folder
        root = root_of(folder)
        if ask and root and root not in islands():  # the handoff would create or change a profile
            question = "Give `%s` its own profile `%s`? Then leave this chat and continue there." % (
                short(root), profile_name(root))
            if ask(question, [{"value": "yes", "label": "Yes", "description": "make the island profile"},
                              {"value": "no", "label": "No", "description": "change nothing"}]) != "yes":
                return "Nothing changed. Run `/island PATH` to give a codebase its own profile without this question."
            result = handoff(folder, current=current)
            if result.ok and leave and leave():
                return result.text + "\nLeaving this chat (/quit sent)."
            return result.text
        return handoff(folder, current=current).text
    path = words_[0]
    if path != "~" and not is_path(path):  # a relative PATH is in the chat's folder, as Tab completion lists it
        path = os.path.join(chat_folder() or os.getcwd(), path)
    return handoff(path, words_[1] if len(words_) > 1 else None, current=current).text


def complete(arg: str = "") -> List[Tuple[str, str]]:
    """The argument completions of /island: (text, meta) rows. "." is the launch folder's codebase, with its
    state (the CAUTION check: an island of a profile, or none); then this profile's newest chat's codebase."""
    if " " in arg.strip():
        return []
    owners = islands()

    def meta(root: Optional[str]) -> str:
        if not root:
            return "the launch folder is in no codebase"
        owner = owners.get(root)
        return "%s: %s" % (short(root), "the island of `%s`" % owner if owner else "no island")
    rows = [(".", meta(launch_root()))]
    recent = recent_root(current_home())
    if recent and recent != launch_root():
        rows.append((short(recent), "this chat's codebase, " + meta(recent).split(": ", 1)[1]))
    return rows + [("status", "this profile's island and checks"), ("--all", "every profile's island")]


def cli_setup(parser) -> None:
    """`hermes islands ...`: the shell form of /island."""
    sub = parser.add_subparsers(dest="islands_action")
    island = sub.add_parser("island", help="Give the codebase at PATH its own profile (the handoff)")
    island.add_argument("path", nargs="?", help="a folder or file in the codebase")
    island.add_argument("name", nargs="?", help="the profile name (default: the repository folder name)")
    status_ = sub.add_parser("status", help="This profile's island, the islands of all profiles and their checks, "
                                            "the codebases of this profile's tool calls, the offer of breadcrumbs")
    status_.add_argument("--all", action="store_true", help="each profile: its island, the codebases of its tool "
                                                            "calls, and a verdict")


def cli_run(args) -> int:
    current = profile_of(current_home())
    action = getattr(args, "islands_action", None)
    path = getattr(args, "path", None) if action == "island" else None
    if path:
        result = handoff(path, getattr(args, "name", None), current=current)
    elif action == "status" and getattr(args, "all", False):
        result = report()
    else:
        result = status(current)
    print(result.text)
    return 0 if result.ok else 1


def _guarded(tracker: "Tracker", name: str):
    def hook(**kw):
        try:
            return getattr(tracker, name)(**kw)
        except Exception:  # the offer never breaks a tool call or a reply
            logging.getLogger(__name__).debug("breadcrumbs island: %s failed", name, exc_info=True)
            return None
    hook.__name__ = name
    return hook


def register(ctx):
    """The hooks of the automatic offer, the /island and /viz slash commands and the `hermes islands` CLI command,
    each where the Hermes context has it. Returns the Tracker, or None for a Hermes with no hooks."""
    t = None
    if hasattr(ctx, "register_hook"):
        t = Tracker()
        ctx.register_hook("on_session_start", _guarded(t, "on_session_start"))
        ctx.register_hook("post_tool_call", _guarded(t, "post_tool_call"))
        ctx.register_hook("transform_llm_output", _guarded(t, "transform_llm_output"))
    if hasattr(ctx, "register_command"):
        extra = {"completer": complete} if "completer" in inspect.signature(ctx.register_command).parameters else {}
        ask = getattr(ctx, "ask_choice", None)

        inject = getattr(ctx, "inject_message", None)

        def leave() -> bool:  # best effort: the CLI queues it; the gateway needs allow_gateway_injection
            try:
                return bool(inject and inject("/quit"))
            except Exception:
                return False

        def island_command(raw_args: str = "", session_id: str = "", platform: str = "") -> str:
            if platform and platform not in LOCAL_SURFACES:  # a messaging platform: the gateway disclaims /island
                return GATEWAY_DISCLAIMER % platform
            return command(raw_args, session_id, ask, leave)
        ctx.register_command("island", island_command, description=COMMAND_HELP, args_hint="[path]", **extra)

        def viz_command(raw_args: str = "", session_id: str = "", platform: str = "") -> str:
            if platform and platform not in LOCAL_SURFACES:
                return VIZ_DISCLAIMER % platform
            return open_viz(session_id=session_id)
        ctx.register_command("viz", viz_command, description=VIZ_HELP)
    if hasattr(ctx, "register_cli_command"):
        ctx.register_cli_command("islands", help="breadcrumbs islands: one codebase per profile",
                                 setup_fn=cli_setup, handler_fn=cli_run,
                                 description="hermes islands island PATH [NAME]: give the codebase at PATH its own "
                                             "profile. hermes islands status [--all]: the islands, their checks, "
                                             "and the codebases of the tool calls of this profile (or of all).")
    return t
