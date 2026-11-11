"""breadcrumbs/kitty.py (the Kitty graphics protocol escapes that Kitty and Ghostty read) and `term`."""
import base64
import contextlib
import io
import os
import re
import stat
import sys
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, kitty

APC = re.compile(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\")


def commands(text):
    return APC.findall(text)


class Image(unittest.TestCase):
    def test_a_big_png_goes_in_chunks_and_decodes_to_the_same_bytes(self):
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 50  # 12808 bytes: 17080 base64 chars, 5 chunks
        cmds = commands(kitty.image(png, cols=120))
        self.assertEqual(len(cmds), 5)
        keys = dict(kv.split("=") for kv in cmds[0][0].split(","))
        self.assertEqual(keys, {"a": "T", "f": "100", "q": "2", "i": "1", "p": "1", "C": "1", "c": "120", "m": "1"})
        self.assertEqual([k for k, _ in cmds[1:]], ["m=1", "m=1", "m=1", "m=0"])  # only the first chunk has the keys
        self.assertTrue(all(len(p) <= kitty.CHUNK and len(p) % 4 == 0 for _, p in cmds))
        self.assertEqual(base64.b64decode("".join(p for _, p in cmds)), png)

    def test_a_small_png_is_one_command_and_no_cols_lets_the_terminal_size_it(self):
        cmds = commands(kitty.image(b"\x89PNG", cols=0))
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0][0], "a=T,f=100,q=2,i=1,p=1,C=1,m=0")

    def test_a_frame_starts_at_home_and_replaces_the_same_placement(self):
        a, b = kitty.frame(b"one", 80), kitty.frame(b"two", 80)
        self.assertTrue(a.startswith("\x1b[H\x1b_G") and b.startswith("\x1b[H\x1b_G"))
        self.assertEqual(commands(a)[0][0], commands(b)[0][0])  # same i and p: the terminal replaces the image

    def test_clear_deletes_every_placement(self):
        self.assertEqual(kitty.clear(), "\x1b_Ga=d,d=A,q=2\x1b\\")

    def test_supported_terminals(self):
        for env in ({"TERM": "xterm-kitty"}, {"TERM": "xterm-ghostty"}, {"TERM_PROGRAM": "ghostty"},
                    {"KITTY_WINDOW_ID": "3", "TERM": "screen"}):
            self.assertTrue(kitty.supported(env), env)
        for env in ({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"}, {"TERM": "dumb"}, {}):
            self.assertFalse(kitty.supported(env), env)


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20

# A stand-in for Chrome: it writes PNG to the --screenshot= path and keeps its arguments, or it fails.
FAKE = r'''#!%s
import sys
args = sys.argv[1:]
with open(%r, "a") as f:
    f.write("\t".join(args) + "\n")
if %r:
    sys.exit("chrome: no display")
out = [a.split("=", 1)[1] for a in args if a.startswith("--screenshot=")][0]
with open(out, "wb") as f:
    f.write(%r)
'''


@unittest.skipIf(os.name == "nt", "the fake browser is a script with a #! line")
class Term(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = os.path.join(self.tmp.name, "calls.txt")
        old = os.environ.get("BREADCRUMBS_CHROME")
        self.addCleanup(lambda: os.environ.pop("BREADCRUMBS_CHROME", None) if old is None
                        else os.environ.__setitem__("BREADCRUMBS_CHROME", old))

    def browser(self, fail=False):
        path = os.path.join(self.tmp.name, "chrome")
        with open(path, "w") as f:
            f.write(FAKE % (sys.executable, self.calls, fail, PNG))
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        os.environ["BREADCRUMBS_CHROME"] = path

    def term(self, *argv, home=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", home or self.tmp.name, "term", "--every", "0", *argv])
        return code, out.getvalue(), err.getvalue()

    def args(self):
        with open(self.calls) as f:
            return [line.rstrip("\n").split("\t") for line in f]

    def test_a_frame_is_the_page_png_in_kitty_escapes_with_the_cursor_hidden_then_shown(self):
        self.browser()
        code, out, err = self.term("--url", "http://127.0.0.1:1/#k=x", "--frames", "2", "--width", "640",
                                   "--height", "400")
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith("\x1b[?25l"))  # cursor hidden first
        self.assertTrue(out.endswith(kitty.clear() + "\x1b[?25h"))  # image cleared, cursor shown at the end
        self.assertEqual(out.count("\x1b[H\x1b_Ga=T"), 2)
        frame = out.split("\x1b[H")[1]
        self.assertEqual(base64.b64decode("".join(p for _, p in commands(frame))), PNG)
        calls = self.args()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "http://127.0.0.1:1/#k=x")
        self.assertIn("--window-size=640,400", calls[0])
        self.assertIn("--headless=new", calls[0])
        profile = [a for a in calls[0] if a.startswith("--user-data-dir=")][0].split("=", 1)[1]
        self.assertFalse(os.path.exists(profile))  # the scratch browser profile goes when the loop ends

    def test_a_failed_frame_goes_to_stderr_and_the_loop_goes_on(self):
        self.browser(fail=True)
        code, out, err = self.term("--url", "http://127.0.0.1:1/", "--frames", "2")
        self.assertEqual(code, 0)
        self.assertEqual(err.count("breadcrumbs term: frame failed"), 2)
        self.assertEqual(len(self.args()), 2)
        self.assertNotIn("\x1b_Ga=T", out)
        self.assertTrue(out.endswith(kitty.clear() + "\x1b[?25h"))

    def test_with_no_url_it_serves_the_store_and_shows_the_page_with_its_token(self):
        from breadcrumbs import view
        from breadcrumbs.hermes import BreadcrumbsProvider
        p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        p.sync_turn("how long should green tea steep", "two minutes", session_id="s1")
        p.shutdown()
        root = os.path.join(self.tmp.name, "breadcrumbs")
        self.browser()
        code, _, err = self.term("--frames", "1", home=root)
        self.assertEqual((code, err), (0, ""))
        url = self.args()[0][-1]
        self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/#k=" + view.token(root) + "&motion=0$")  # one picture: no glide to wait for


if __name__ == "__main__":
    unittest.main()
