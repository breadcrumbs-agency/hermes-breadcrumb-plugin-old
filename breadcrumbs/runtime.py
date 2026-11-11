"""Runtime choices: garbage collection around bulk work, and GraalPy for long batch jobs.

GC: CPython's cyclic collector scans every container that a bulk load makes. no_gc() turns it
off for one block and restores the old state (reference counting still frees memory). With the
old dict index (one object per blob), a store open over 1.78M blobs took 1.18 s with the
collector on and 0.66-0.77 s with it off.

GraalPy: the CLI flag --graal runs the same command under GraalPy with these options:
  --experimental-options --python.OverallocateLiteralLists=false   (25.0.2 over-allocates lists)
  --vm.Xmx<BREADCRUMBS_GRAAL_HEAP, default 4g>
Measured on 246,750 turns into one store (the stores are byte-identical):
  CPython 3.14 + google-crc32c (C)    35.5 s ingest   fastest
  GraalPy 25.0.2, pure CRC32C         74.8 s          the JIT runs the pure-Python work 2.5x faster
  CPython 3.14, pure CRC32C          187.8 s
So --graal pays for long batch jobs (backfill, verify, benchmarks) when no C CRC32C is
installed. With google-crc32c, or for short jobs (GraalPy starts in ~0.5 s with imports),
CPython is faster. Hermes itself runs on CPython, so the memory provider always does too.
GraalPy 25.0.2 has a GIL, so threads give no gain there.
"""

import contextlib
import gc
import glob
import os
import shutil


def graal_options():
    return ["--experimental-options", "--python.OverallocateLiteralLists=false",
            "--vm.Xmx" + os.environ.get("BREADCRUMBS_GRAAL_HEAP", "4g")]


def find_graalpy():
    """$BREADCRUMBS_GRAALPY, else graalpy on PATH, else the newest pyenv GraalPy; None if absent."""
    env = os.environ.get("BREADCRUMBS_GRAALPY")
    if env:
        return env if os.access(env, os.X_OK) else None
    found = [shutil.which("graalpy")]
    found += sorted(glob.glob(os.path.expanduser("~/.pyenv/versions/graalpy-*/bin/graalpy")), reverse=True)
    return next((c for c in found if c and os.access(c, os.X_OK)), None)


def graal_argv(exe, args):
    return [exe, *graal_options(), "-m", "breadcrumbs", *args]


@contextlib.contextmanager
def no_gc():
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()
