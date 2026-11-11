"""The intercept: ACE takes Hermes's built-in compressor in each profile whose memory is breadcrumbs."""
import json
import os
import tempfile
import unittest
from unittest import mock

from breadcrumbs import ace, intercept
from test_ace import SYSTEM, chat


class Engine:
    """Hermes's ContextEngine: the built-in inherits select_context from it (None keeps the request)."""

    def select_context(self, request_messages, **kw):
        return None


class Builtin(Engine):
    """A stand-in for Hermes's ContextCompressor, with the hooks that the intercept wraps."""

    def __init__(self):
        self.context_length, self.threshold_tokens, self._session_id = 32000, 2000, "s1"
        self.calls = []

    def compress(self, messages, *args, **kw):
        self.calls.append("compress")
        return messages[:1]

    def update_from_response(self, usage):
        self.calls.append("usage")

    def _generate_summary(self, *args, **kw):
        self.calls.append("summary")
        return "a model summary"


class InterceptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.cls = type("Compressor", (Builtin,), {})  # a fresh class for each test
        for patch in (mock.patch.object(intercept, "_compressor_class", return_value=self.cls),
                      mock.patch.dict(os.environ, {"HERMES_HOME": self.home})):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(intercept.uninstall, self.cls)
        self.req = [SYSTEM] + chat(40)

    def crumbs_memory(self, name="bc", intercept=True):
        """A profile whose memory provider is breadcrumbs. intercept: what its config.json says (None: no file)."""
        os.makedirs(os.path.join(self.home, "plugins", name))
        if intercept is not None:
            os.makedirs(os.path.join(self.home, "breadcrumbs"), exist_ok=True)
            with open(os.path.join(self.home, "breadcrumbs", "config.json"), "w") as f:
                json.dump({"intercept": intercept}, f)
        with open(os.path.join(self.home, "plugins", name, "__init__.py"), "w") as f:
            f.write('"""breadcrumbs memory provider (plugin shim)."""\n')
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            f.write("model: x\nmemory:\n  provider: %s\n" % name)

    def test_install_wraps_the_hooks_and_uninstall_puts_them_back(self):
        own = lambda self, messages, *a, **kw: "own"  # noqa: E731
        cls = type("Own", (Builtin,), {"compress": own})
        self.assertEqual(intercept.install(cls), [])
        self.assertIsNot(cls.__dict__["compress"], own)
        self.assertIn("select_context", cls.__dict__)  # an inherited hook is wrapped on the class too
        intercept.uninstall(cls)
        self.assertIs(cls.__dict__["compress"], own)
        self.assertNotIn("select_context", cls.__dict__)
        self.assertNotIn(intercept.SAVED, cls.__dict__)

    def test_a_second_install_keeps_the_first_originals(self):
        intercept.install()
        intercept.install()  # a reload of the plugin
        intercept.uninstall(self.cls)
        self.assertEqual([h for h in intercept.HOOKS if h in self.cls.__dict__], [])
        self.assertEqual(self.cls()._generate_summary(), "a model summary")

    def test_a_profile_with_breadcrumbs_memory_gets_the_layer_and_no_summary_model(self):
        self.crumbs_memory()
        self.assertEqual(intercept.install(), [])
        cc = self.cls()
        out = cc.select_context(self.req)
        self.assertTrue(out and out[1]["content"].startswith(ace.LAYER_HEAD), out and out[1]["content"][:60])
        self.assertIsNone(cc._generate_summary(["turns"]))
        cc.update_from_response({"prompt_tokens": 1234})
        self.assertEqual(cc.calls, ["usage"])  # the built-in still counts; it wrote no summary
        with open(os.path.join(self.home, "breadcrumbs", "ace.log")) as f:
            self.assertIn("intercept", f.read())

    def test_compress_folds_with_ace_and_counts_the_compaction(self):
        self.crumbs_memory()
        intercept.install()
        cc = self.cls()
        out = cc.compress(self.req)
        self.assertNotEqual(out, self.req[:1])
        self.assertTrue(out[1]["content"].startswith(ace.LAYER_HEAD))
        self.assertEqual((cc.calls, getattr(cc, "compression_count", 0)), ([], 1))

    def test_a_profile_without_breadcrumbs_memory_keeps_the_builtin(self):
        with open(os.path.join(self.home, "config.yaml"), "w") as f:
            f.write("memory:\n  provider: holographic\n")
        intercept.install()
        cc = self.cls()
        self.assertIsNone(cc.select_context(self.req))
        self.assertEqual(cc._generate_summary(), "a model summary")
        self.assertEqual(cc.compress(self.req), self.req[:1])

    def test_a_profile_that_does_not_opt_in_keeps_the_builtin(self):
        """The intercept changes Hermes's compressor for every profile of the gateway, through private core
        imports. A profile gets it only when its config.json says "intercept": true."""
        self.crumbs_memory(intercept=None)
        intercept.install()
        cc = self.cls()
        self.assertIsNone(cc.select_context(self.req))
        self.assertEqual(cc._generate_summary(), "a model summary")
        self.assertEqual(cc.compress(self.req), self.req[:1])

    def test_a_profile_that_opts_out_keeps_the_builtin(self):
        self.crumbs_memory(intercept=False)
        intercept.install()
        cc = self.cls()
        self.assertIsNone(cc.select_context(self.req))
        self.assertEqual(cc._generate_summary(), "a model summary")

    def test_in_the_plugin_host_install_does_nothing(self):
        with mock.patch.dict(os.environ, {intercept.HOST_ENV: "1"}), self.assertLogs(intercept.log, "WARNING") as logs:
            self.assertIsNone(intercept.install())
        self.assertIn("plugin host process", logs.output[0])
        self.assertNotIn(intercept.SAVED, self.cls.__dict__)

    def test_outside_hermes_install_does_nothing(self):
        with mock.patch.object(intercept, "_compressor_class", return_value=None):
            self.assertIsNone(intercept.install())


if __name__ == "__main__":
    unittest.main()
