"""`open` (breadcrumbs/launch.py): the explorer in the default browser, or in a new Kitty or Ghostty window.

Subprocess and webbrowser are mocked: no window and no browser opens, and no app has to be installed. The token of
a test home is read from its own view.token at run time; it is never printed.
"""
import contextlib
import io
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, launch, term, view
from breadcrumbs.hermes import BreadcrumbsProvider

# The start of the command line for each (macOS?, app); `term` on the home follows.
HEAD = {
    (True, "kitty"): ["open", "-na", "kitty.app", "--args"],
    (True, "ghostty"): ["open", "-na", "Ghostty.app", "--args", "-e"],
    (False, "kitty"): ["kitty"],
    (False, "ghostty"): ["ghostty", "-e"],
}
NAME = {"kitty": "Kitty", "ghostty": "Ghostty"}
HEAD_APPS = ("kitty", "ghostty")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def store(base):
    """A small store under base; returns its home (the folder that holds view.token)."""
    p = BreadcrumbsProvider()
    p.initialize("s1", hermes_home=base, platform="cli", agent_context="primary")
    p.sync_turn("how long should green tea steep", "two minutes", session_id="s1")
    p.shutdown()
    return os.path.join(base, "breadcrumbs")


class FakeServer:
    """What view.make_server returns, but serve_forever returns at once."""

    def __init__(self, port):
        self.server_address = ("127.0.0.1", port)
        self.served = self.closed = False

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PYTHONPATH", None)

    def run_open(self, *argv, home=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--home", home or self.home, "open", *argv])
        return code, out.getvalue(), err.getvalue()

    def one_line(self, text):
        self.assertEqual(len(text.splitlines()), 1, text)
        self.assertNotIn("Traceback", text)


class Windows(Base):
    def setUp(self):
        super().setUp()
        self.term = ["env", "PYTHONPATH=" + launch.REPO, sys.executable, "-m", "breadcrumbs", "--home", self.home,
                     "term"]

    def test_print_shows_the_command_line_for_each_app_on_macos_and_linux_and_runs_nothing(self):
        for (mac, app), head in HEAD.items():
            with self.subTest(mac=mac, app=app), mock.patch.object(launch, "is_mac", return_value=mac), \
                    mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen, \
                    mock.patch("webbrowser.open") as browser:
                code, out, err = self.run_open(app, "--print")
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(shlex.split(out), head + self.term)
                self.one_line(out)
                for m in (run, popen, browser):
                    m.assert_not_called()

    def test_a_set_PYTHONPATH_stays_in_the_command_of_the_window(self):
        os.environ["PYTHONPATH"] = "/somewhere"
        code, out, err = self.run_open("kitty", "--print")
        self.assertIn("PYTHONPATH=" + launch.REPO + os.pathsep + "/somewhere", shlex.split(out))

    def test_a_run_on_macos_starts_the_app_with_open(self):
        for app in HEAD_APPS:
            with self.subTest(app=app), mock.patch.object(launch, "is_mac", return_value=True), \
                    mock.patch.object(term, "find_chrome", return_value="/chrome"), \
                    mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, None, "")) as run:
                code, out, err = self.run_open(app)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(run.call_args.args[0], HEAD[(True, app)] + self.term)
                self.one_line(out)
                self.assertIn(NAME[app], out)

    def test_a_run_on_linux_starts_the_program_and_does_not_wait_for_the_window(self):
        for app in HEAD_APPS:
            proc = mock.Mock()
            proc.wait.side_effect = subprocess.TimeoutExpired("x", launch.GRACE)  # still running: the window is up
            with self.subTest(app=app), mock.patch.object(launch, "is_mac", return_value=False), \
                    mock.patch.object(term, "find_chrome", return_value="/chrome"), \
                    mock.patch("subprocess.Popen", return_value=proc) as popen:
                code, out, err = self.run_open(app)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(popen.call_args.args[0], HEAD[(False, app)] + self.term)
                self.assertTrue(popen.call_args.kwargs["start_new_session"])  # the window outlives this command
                self.one_line(out)

    def test_an_app_that_dies_at_once_on_linux_is_exit_1_and_one_line(self):
        proc = mock.Mock()
        proc.wait.return_value = 1
        with mock.patch.object(launch, "is_mac", return_value=False), \
                mock.patch.object(term, "find_chrome", return_value="/chrome"), \
                mock.patch("subprocess.Popen", return_value=proc):
            code, out, err = self.run_open("kitty")
        self.assertEqual((code, out), (1, ""))
        self.one_line(err)
        self.assertIn("status 1", err)

    def test_a_missing_app_is_exit_2_with_one_line_that_says_how_to_install_it(self):
        cases = ((True, "kitty", "Kitty is not installed. Install: brew install --cask kitty"),
                 (True, "ghostty", "Ghostty is not installed. Install: brew install --cask ghostty"),
                 (False, "kitty", "kitty is not on PATH. Install: "),
                 (False, "ghostty", "ghostty is not on PATH. Install: "))
        for mac, app, text in cases:
            gone = subprocess.CompletedProcess([], 1, None, "Unable to find application named 'x'\n")
            with self.subTest(mac=mac, app=app), mock.patch.object(launch, "is_mac", return_value=mac), \
                    mock.patch.object(term, "find_chrome", return_value="/chrome"), \
                    mock.patch("subprocess.run", return_value=gone), \
                    mock.patch("subprocess.Popen", side_effect=FileNotFoundError(2, "No such file or directory")):
                code, out, err = self.run_open(app)
                self.assertEqual((code, out), (2, ""))
                self.one_line(err)
                self.assertIn(text, err)
                self.assertIn("https://", err)

    def test_open_that_fails_for_another_reason_is_exit_1_with_its_own_words(self):
        bad = subprocess.CompletedProcess([], 1, None, "The application cannot be opened for an unexpected reason\n")
        with mock.patch.object(launch, "is_mac", return_value=True), \
                mock.patch.object(term, "find_chrome", return_value="/chrome"), mock.patch("subprocess.run", return_value=bad):
            code, out, err = self.run_open("ghostty")
        self.assertEqual((code, out), (1, ""))
        self.one_line(err)
        self.assertIn("cannot be opened", err)

    def test_term_draws_with_chrome_so_without_chrome_no_window_opens(self):
        with mock.patch.object(launch, "is_mac", return_value=True), mock.patch.object(term, "find_chrome", return_value=None), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, err = self.run_open("kitty")
        self.assertEqual((code, out), (2, ""))
        self.one_line(err)
        self.assertIn("BREADCRUMBS_CHROME", err)
        run.assert_not_called()
        popen.assert_not_called()



class Web(Base):
    def setUp(self):
        super().setUp()
        self.home = store(self.tmp.name)
        self.key = view.token(self.home)

    def serve(self, home):
        """A real server for home on a free port, in a thread; returns the port."""
        server = view.make_server(home, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server.server_address[1]

    def test_print_shows_the_address_without_the_token_and_runs_nothing(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        for argv, port in (((), launch.PORT), (("web",), launch.PORT), (("--port", "9999"), 9999)):
            with self.subTest(argv=argv), mock.patch("webbrowser.open") as browser, \
                    mock.patch.object(view, "make_server") as make, mock.patch("subprocess.run") as run:
                code, out, err = self.run_open(*argv, "--print", home=empty)
                self.assertEqual((code, err), (0, ""))
                self.assertIn("http://127.0.0.1:%d/" % port, out)
                self.one_line(out)
                for m in (browser, make, run):
                    m.assert_not_called()
        code, out, err = self.run_open("--print")
        self.assertNotIn(self.key, out)
        self.assertEqual(os.listdir(empty), [])  # --print made no file, not even a view.token

    def test_the_usage_text_names_the_command(self):
        self.assertIn("  open [web|kitty|ghostty] [--print]", cli.__doc__ or "")

    def test_with_no_server_it_starts_one_here_and_the_browser_gets_the_token(self):
        port = free_port()
        fake = FakeServer(port)
        with mock.patch.object(launch, "PORT", port), mock.patch.object(view, "make_server", return_value=fake) as make, \
                mock.patch("webbrowser.open", return_value=True) as browser:
            code, out, err = self.run_open()
        self.assertEqual((code, err), (0, ""))
        make.assert_called_once_with(self.home, port=port)
        browser.assert_called_once_with("http://127.0.0.1:%d/#k=%s" % (port, self.key))
        self.assertIn("http://127.0.0.1:%d/" % port, out)
        self.assertNotIn(self.key, out + err)  # the address is printed without the token
        self.assertTrue(fake.served and fake.closed)  # it served until Ctrl-C, then closed

    def test_a_server_that_takes_this_homes_token_is_reused(self):
        port = self.serve(self.home)
        with mock.patch.object(view, "make_server", side_effect=AssertionError("a second server")), \
                mock.patch("webbrowser.open", return_value=True) as browser:
            code, out, err = self.run_open("--port", str(port))
        self.assertEqual((code, err), (0, ""))
        browser.assert_called_once_with("http://127.0.0.1:%d/#k=%s" % (port, self.key))
        self.assertIn("already running", out)
        self.assertNotIn(self.key, out)

    def test_a_server_of_another_home_is_never_reused_and_a_busy_default_port_gives_a_free_one(self):
        busy = self.serve(store(os.path.join(self.tmp.name, "other")))
        real, seen = view.make_server, []

        def make(home, port):
            seen.append(port)
            server = real(home, port=port)
            server.serve_forever = lambda poll_interval=0.5: None  # return at once, as a Ctrl-C would
            return server

        with mock.patch.object(launch, "PORT", busy), mock.patch.object(view, "make_server", side_effect=make), \
                mock.patch("webbrowser.open", return_value=True) as browser:
            code, out, err = self.run_open()
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(seen, [busy, 0])  # the busy port first, then any free port
            url = browser.call_args.args[0]
            self.assertTrue(url.startswith("http://127.0.0.1:") and url.endswith("/#k=" + self.key), "wrong address")
            self.assertNotIn(":%d/" % busy, url)
            seen.clear()
            browser.reset_mock()
            code, out, err = self.run_open("--port", str(busy))  # a port that was asked for is not replaced
        self.assertEqual((code, out), (1, ""))
        self.one_line(err)
        self.assertIn("port %d" % busy, err)
        browser.assert_not_called()

    def test_a_home_with_no_store_is_exit_1_and_one_line_and_makes_no_file(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.mkdir(empty)
        with mock.patch.object(launch, "PORT", free_port()), mock.patch("webbrowser.open") as browser:
            code, out, err = self.run_open(home=empty)
        self.assertEqual((code, out), (1, ""))
        self.one_line(err)
        self.assertIn("no store", err)
        self.assertEqual(os.listdir(empty), [])
        browser.assert_not_called()

    def test_when_no_browser_starts_the_server_closes_and_the_exit_is_1(self):
        fake = FakeServer(free_port())
        with mock.patch.object(launch, "PORT", fake.server_address[1]), \
                mock.patch.object(view, "make_server", return_value=fake), mock.patch("webbrowser.open", return_value=False):
            code, out, err = self.run_open()
        self.assertEqual((code, out), (1, ""))
        self.one_line(err)
        self.assertNotIn(self.key, err)
        self.assertTrue(fake.closed and not fake.served)


if __name__ == "__main__":
    unittest.main()
