import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest

from breadcrumbs import runtime
from breadcrumbs.store import Store

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RuntimeTest(unittest.TestCase):
    def test_graal_argv_carries_the_options_and_the_command(self):
        argv = runtime.graal_argv("/opt/graalpy/bin/graalpy", ["--home", "x", "stats"])
        self.assertEqual(argv[0], "/opt/graalpy/bin/graalpy")
        self.assertIn("--python.OverallocateLiteralLists=false", argv)
        self.assertEqual(argv[-5:], ["-m", "breadcrumbs", "--home", "x", "stats"])

    def test_env_override_wins(self):
        with tempfile.NamedTemporaryFile(suffix="graalpy") as f:
            os.chmod(f.name, 0o755)
            old = os.environ.get("BREADCRUMBS_GRAALPY")
            os.environ["BREADCRUMBS_GRAALPY"] = f.name
            try:
                self.assertEqual(runtime.find_graalpy(), f.name)
            finally:
                if old is None:
                    del os.environ["BREADCRUMBS_GRAALPY"]
                else:
                    os.environ["BREADCRUMBS_GRAALPY"] = old

    @unittest.skipUnless(sys.implementation.name == "cpython" and runtime.find_graalpy(), "needs CPython plus a GraalPy")
    def test_cli_reexecs_under_graalpy(self):
        with tempfile.TemporaryDirectory() as home:
            out = subprocess.run([sys.executable, "-m", "breadcrumbs", "--graal", "--home", home, "stats"],
                                 cwd=REPO, capture_output=True, text=True, check=True).stdout
            self.assertEqual(json.loads(out)["runtime"], "graalpy")


class GcTest(unittest.TestCase):
    def test_store_open_restores_the_gc_state(self):
        with tempfile.TemporaryDirectory() as d:
            with Store(d) as s:
                s.put(b"one")
            for enabled in (True, False):
                (gc.enable if enabled else gc.disable)()
                try:
                    Store(d).close()
                    self.assertEqual(gc.isenabled(), enabled)
                finally:
                    gc.enable()


if __name__ == "__main__":
    unittest.main()
