"""`term`: the explorer page in Kitty or Ghostty. Headless Chrome takes a PNG of the page; kitty.frame draws it.

Each frame starts a fresh headless Chrome with a scratch profile (never the user's) and a virtual time
budget, so the page's API calls and its layout finish before the shot. A failed frame goes to stderr and
the loop goes on. Ctrl-C (or the last of --frames) clears the image and shows the cursor again.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import kitty

CHROMES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
           "/Applications/Chromium.app/Contents/MacOS/Chromium")
NAMES = ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")


def find_chrome(env=os.environ):
    """$BREADCRUMBS_CHROME, else Chrome or Chromium in /Applications, else one on PATH (None: not found)."""
    if env.get("BREADCRUMBS_CHROME"):
        return env["BREADCRUMBS_CHROME"]
    for path in CHROMES:
        if os.access(path, os.X_OK):
            return path
    for name in NAMES:
        path = shutil.which(name)
        if path:
            return path
    return None


def shot(chrome, url, width, height, budget_ms, profile, timeout=120):
    """One PNG of url, width x height pixels, after budget_ms of the page's (virtual) time."""
    out = os.path.join(profile, "frame.png")
    if os.path.exists(out):
        os.remove(out)
    argv = [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", "--force-device-scale-factor=1", "--user-data-dir=" + profile,
            "--virtual-time-budget=%d" % budget_ms, "--window-size=%d,%d" % (width, height),
            "--screenshot=" + out, url]
    r = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    if not os.path.exists(out):
        tail = r.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no PNG"]
        raise RuntimeError("chrome exit %d: %s" % (r.returncode, tail[0]))
    with open(out, "rb") as f:
        return f.read()


def run(url, every=10.0, width=1280, height=800, budget_ms=8000, frames=0, out=None, err=None):
    """Draw url every `every` seconds until Ctrl-C (or `frames` frames when frames > 0)."""
    out, err = out or sys.stdout, err or sys.stderr
    chrome = find_chrome()
    if not chrome:
        print("breadcrumbs term: no Chrome or Chromium found (set BREADCRUMBS_CHROME)", file=err)
        return 1
    if out.isatty() and not kitty.supported():
        print("breadcrumbs term: this terminal may not read the Kitty graphics protocol (Kitty, Ghostty)",
              file=err)
    profile = tempfile.mkdtemp(prefix="breadcrumbs-term-")
    out.write("\x1b[?25l")  # hide the cursor
    out.flush()
    n = 0
    try:
        while True:
            t = time.monotonic()
            try:
                png = shot(chrome, url, width, height, budget_ms, profile)
                out.write(kitty.frame(png, shutil.get_terminal_size().columns))
                out.flush()
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                print("breadcrumbs term: frame failed: %s" % e, file=err)
            n += 1
            if frames and n >= frames:
                break
            time.sleep(max(0.0, every - (time.monotonic() - t)))
    except KeyboardInterrupt:
        pass
    finally:
        out.write(kitty.clear() + "\x1b[?25h")
        out.flush()
        shutil.rmtree(profile, ignore_errors=True)
    return 0
