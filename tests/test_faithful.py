"""bench/faithful.py: the census of the catalog's memory plugins reads hooks and floors with ast only."""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FAITHFUL = os.path.join(HERE, "..", "bench", "faithful.py")  # bench/ is not in the release: the tests skip where it is missing
faithful = None
if os.path.isfile(FAITHFUL):
    _spec = importlib.util.spec_from_file_location("faithful", FAITHFUL)
    assert _spec is not None and _spec.loader is not None
    faithful = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(faithful)

ABC = ["name", "is_available", "initialize", "prefetch", "sync_turn", "get_tool_schemas", "on_memory_write",
       "on_pre_compress", "system_prompt_block", "shutdown"]

BASE = '''
from agent.memory_provider import MemoryProvider
import psycopg

class Base(MemoryProvider):
    def initialize(self, session_id, **kw): ...
    def prefetch(self, query, *, session_id=""): return ""
'''
PROVIDER = '''
import sqlite3
from .base import Base

class Kettle(Base):
    @property
    def name(self): return "kettle"
    def sync_turn(self, u, a, *, session_id=""): ...
    def helper(self): ...
    def get_tool_schemas(self):
        return [{"name": "kettle_find", "description": "find", "parameters": {"type": "object"}}]

def register(ctx):
    ctx.register_memory_provider(Kettle())
    ctx.register_hook("pre_llm_call", lambda **kw: None)
'''
LOOSE = '''
class Loose:
    def prefetch(self, query, *, session_id=""): return ""
    def on_memory_write(self, action, target, content): ...

def register(ctx):
    ctx.register_memory_provider(Loose())
'''
FORWARDER = '''
class BreadcrumbsProvider:
    def initialize(self, session_id, **kw):
        self.slot.start(session_id, kw)
    def get_tool_schemas(self):
        return self.slot.schemas("p", set())
    def prefetch(self, query, *, session_id=""):
        return self._slot_recall(query, session_id)
    def _slot_recall(self, query, session_id):
        return self.slot.call("prefetch", query, session_id=session_id)
    def sync_turn(self, u, a, *, session_id=""):
        self.slot.call("sync_turn", u, a, session_id=session_id)
    def _switch(self):
        self.slot.call("shutdown")
    def shutdown(self):
        pass
'''


@unittest.skipUnless(faithful, "bench/faithful.py is not in the release")
class CensusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def plugin(self, files):
        root = os.path.join(self.tmp.name, "p%d" % len(os.listdir(self.tmp.name)))
        for path, text in files.items():
            full = os.path.join(root, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(text)
        return faithful.read_plugin(root, ABC)

    def test_hooks_come_from_the_registered_class_and_its_bases_in_the_plugin(self):
        got = self.plugin({"base.py": BASE, "__init__.py": PROVIDER})
        self.assertEqual(got["providers"], ["Kettle"])
        self.assertEqual(got["hooks"], ["get_tool_schemas", "initialize", "name", "prefetch", "sync_turn"])
        self.assertEqual(got["plugin_hooks"], ["pre_llm_call"])
        self.assertEqual(got["tools"], ["kettle_find"])
        self.assertEqual(got["floor"], ["postgres", "sqlite"])

    def test_a_registered_class_without_the_base_class_counts(self):
        got = self.plugin({"__init__.py": LOOSE})
        self.assertEqual((got["providers"], got["hooks"]), (["Loose"], ["on_memory_write", "prefetch"]))

    def test_declared_dependencies_add_to_the_floor(self):
        got = self.plugin({"__init__.py": LOOSE, "pyproject.toml":
                           '[project]\nname = "x"\ndependencies = ["psycopg[binary]>=3.1", "httpx"]\n'})
        self.assertEqual(got["deps"], ["httpx", "psycopg"])
        self.assertEqual(got["floor"], ["postgres", "service"])

    def test_only_the_plugins_own_code_is_fetched(self):
        want = [p for p in ("sub/__init__.py", "sub/pkg/store.py", "sub/pyproject.toml", "sub/requirements.txt",
                            "sub/tests/test_x.py", "sub/test_y.py", "other/x.py", "sub/README.md")
                if faithful.wanted(p, "sub")]
        self.assertEqual(want, ["sub/__init__.py", "sub/pkg/store.py", "sub/pyproject.toml", "sub/requirements.txt"])

    def test_a_hook_is_forwarded_only_where_the_hook_itself_reaches_the_slot(self):
        path = os.path.join(self.tmp.name, "hermes.py")
        with open(path, "w") as f:
            f.write(FORWARDER)
        self.assertEqual(faithful.forwarded(path), ["get_tool_schemas", "initialize", "prefetch", "sync_turn"])


if __name__ == "__main__":
    unittest.main()
