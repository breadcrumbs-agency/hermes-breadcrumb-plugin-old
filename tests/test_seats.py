import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, seats
from breadcrumbs.crumbs import get_crumb
from breadcrumbs.history import Refs, log
from breadcrumbs.index import Index
from breadcrumbs.lines import get_text
from breadcrumbs.store import Store

PROVIDER = '''from agent.memory_provider import MemoryProvider
from .store import Facts


class Demo(MemoryProvider):
    pass
'''


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env).stdout.decode()


def facts_db(path, rows, keep_open=False):
    """A database with the table that the holographic provider writes (the columns harvest reads)."""
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL").fetchone()  # read the row: on GraalPy an open statement blocks commit
    db.execute("CREATE TABLE facts (fact_id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL UNIQUE, "
               "category TEXT DEFAULT 'general', tags TEXT DEFAULT '', trust_score REAL DEFAULT 0.5)")
    db.executemany("INSERT INTO facts (content, category) VALUES (?, ?)", rows)
    db.commit()
    if keep_open:
        return db
    db.close()
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "hermes")  # the system's side: a HERMES_HOME
        os.makedirs(self.home)
        root = os.path.join(self.tmp.name, "bc")  # the breadcrumbs side
        self.store = Store(os.path.join(root, "store"))
        self.index = Index(os.path.join(root, "index.db"))
        self.refs = Refs(os.path.join(root, "refs"))
        self.root = root
        self.addCleanup(self.close)

    def close(self):
        if self.store is not None:
            self.index.close()
            self.store.close()
            self.store = None

    def reopen(self):
        self.close()
        self.store = Store(os.path.join(self.root, "store"))
        self.index = Index(os.path.join(self.root, "index.db"))

    def write(self, rel, text):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path


@unittest.skipUnless(shutil.which("git"), "git is needed to clone from history")
class CloneTest(Base):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp.name, "agent")
        os.makedirs(os.path.join(self.repo, "plugins", "memory", "demo"))
        git(self.repo, "init", "-q")
        files = {"LICENSE": "MIT License\n", "plugins/memory/demo/__init__.py": PROVIDER,
                 "plugins/memory/demo/store.py": "class Facts:\n    pass\n",
                 "plugins/memory/kept/__init__.py": PROVIDER,
                 "plugins/memory/coreonly/__init__.py": PROVIDER.replace(
                     "from .store import Facts", "from plugins.memory.coreonly.store import Facts")}
        for rel, text in files.items():
            path = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "add providers")
        self.last = git(self.repo, "rev-parse", "HEAD").strip()
        git(self.repo, "rm", "-q", "-r", "plugins/memory/demo", "plugins/memory/coreonly")
        git(self.repo, "commit", "-q", "-m", "demo leaves core")
        self.gone = git(self.repo, "rev-parse", "HEAD").strip()
        self.plugins = os.path.join(self.home, "plugins")

    def test_a_provider_that_left_the_core_comes_back_from_the_last_core_commit_with_its_provenance(self):
        path = seats.clone("demo", self.plugins, repo=self.repo)
        self.assertEqual(path, os.path.join(self.plugins, "demo"))
        with open(os.path.join(path, "__init__.py")) as f:
            self.assertEqual(f.read(), PROVIDER)
        with open(os.path.join(path, "CLONED.json")) as f:
            meta = json.load(f)
        self.assertEqual((meta["commit"], meta["removed_in"]), (self.last, self.gone))
        self.assertEqual(sorted(meta["files"]), ["LICENSE", "__init__.py", "store.py"])
        for rel, digest in meta["files"].items():  # the hashes name the bytes that were written
            with open(os.path.join(path, rel), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest)

    def test_clone_refuses_a_provider_still_in_the_core_one_never_there_and_an_existing_dir(self):
        with self.assertRaises(LookupError):
            seats.clone("kept", self.plugins, repo=self.repo)  # still in the core: reuse it
        with self.assertRaises(LookupError):
            seats.clone("absent", self.plugins, repo=self.repo)
        seats.clone("demo", self.plugins, repo=self.repo)
        with self.assertRaises(FileExistsError):
            seats.clone("demo", self.plugins, repo=self.repo)

    def test_a_copy_that_imports_its_core_path_is_refused_and_leaves_nothing(self):
        with self.assertRaises(LookupError) as e:
            seats.clone("coreonly", self.plugins, repo=self.repo)
        self.assertIn("plugin catalog", str(e.exception))
        self.assertEqual(os.listdir(self.plugins) if os.path.isdir(self.plugins) else [], [])

    def test_the_cli_clones_into_the_plugins_of_a_hermes_home(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", self.root, "seat", "clone", "demo", self.home, "--repo", self.repo])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.plugins, "demo", "store.py")))
        self.assertIn(self.last[:10], out.getvalue())


class SnapTest(Base):
    def test_a_snap_keeps_the_exact_state_bytes_and_restore_gives_them_back(self):
        writer = facts_db(os.path.join(self.home, "memory_store.db"),
                          [("the kettle descales with citric acid", "general")], keep_open=True)
        self.addCleanup(writer.close)  # open: the row is still in the write-ahead log
        sid = seats.snap(self.store, self.refs, "holographic", self.home)
        snap = get_crumb(self.store, sid)
        self.assertEqual(snap.kind, "snap")
        files = seats.manifest(self.store, sid)
        self.assertEqual([f[0] for f in files], ["memory_store.db"])
        self.assertEqual(get_crumb(self.store, self.refs.get("seat-holographic")).kids, [sid])
        dest = os.path.join(self.tmp.name, "other")
        written = seats.restore(self.store, sid, dest)
        self.assertEqual(written, [os.path.join(dest, "memory_store.db")])
        with open(written[0], "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), files[0][1])
        db = sqlite3.connect(written[0])
        self.assertEqual(db.execute("SELECT content FROM facts").fetchall(), [("the kettle descales with citric acid",)])
        db.close()

    def test_a_snap_walks_a_state_dir_and_restore_refuses_to_replace_without_force(self):
        self.write("byterover/.brv/context-tree/tools/kettle.md", "# Kettle\nDescale monthly.\n")
        self.write("byterover/.brv/context-tree/index.md", "# Index\n")
        sid = seats.snap(self.store, self.refs, "byterover", self.home)
        names = [f[0] for f in seats.manifest(self.store, sid)]
        self.assertEqual(names, ["byterover/.brv/context-tree/index.md",
                                 "byterover/.brv/context-tree/tools/kettle.md"])
        with self.assertRaises(FileExistsError):
            seats.restore(self.store, sid, self.home)
        self.assertEqual(len(seats.restore(self.store, sid, self.home, force=True)), 2)

    def test_a_restored_database_drops_the_old_write_ahead_log_next_to_it(self):
        path = os.path.join(self.home, "memory_store.db")
        facts_db(path, [("tea steeps four minutes", "general")])
        sid = seats.snap(self.store, self.refs, "holographic", self.home)
        db = sqlite3.connect(path)  # later rows that are still in the log when the restore comes
        db.execute("PRAGMA journal_mode=WAL").fetchone()
        db.execute("PRAGMA wal_autocheckpoint=0").fetchone()
        db.execute("INSERT INTO facts (content) VALUES ('a later fact')")
        db.commit()
        side = path + "-wal"
        with open(side, "rb") as f:
            wal = f.read()
        db.close()
        with open(side, "wb") as f:  # the log as a crash leaves it
            f.write(wal)
        seats.restore(self.store, sid, self.home, force=True)
        db = sqlite3.connect(path)
        self.assertEqual(db.execute("SELECT content FROM facts").fetchall(), [("tea steeps four minutes",)])
        db.close()

    def test_restore_never_writes_outside_its_home(self):
        from breadcrumbs.crumbs import Crumb, put_crumb
        from breadcrumbs.lines import put_text
        doc = put_text(self.store, b"x\n")
        kid = put_crumb(self.store, Crumb("file", "x", ranges=[[doc, 0, 1]]))
        for name in ("../escape.txt", "/abs/escape.txt", "~/../escape.txt"):  # a snap from another agent
            line = json.dumps([name, hashlib.sha256(b"x\n").hexdigest(), 2]) + "\n"
            bad = put_crumb(self.store, Crumb("snap", "bad", ranges=[[put_text(self.store, line.encode()), 0, 1]],
                                              kids=[kid]))
            with self.assertRaises(ValueError):
                seats.restore(self.store, bad, os.path.join(self.tmp.name, "dest"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "escape.txt")))

    def test_a_published_snap_goes_to_another_store_in_a_car_file_and_restores_there(self):
        from breadcrumbs import share
        facts_db(os.path.join(self.home, "memory_store.db"), [("the kettle descales with citric acid", "general")])
        self.write("byterover/.brv/context-tree/tools/kettle.md", "# Kettle\nDescale monthly.\n")
        sid = seats.snap(self.store, self.refs, "holographic", self.home, ["memory_store.db", "byterover"])
        car = os.path.join(self.tmp.name, "seat.car")
        share.export_car(self.store, [share.publish(self.store, sid, deep=True)], car)
        other = os.path.join(self.tmp.name, "other")
        store2, index2 = Store(os.path.join(other, "store")), Index(os.path.join(other, "index.db"))
        self.addCleanup(store2.close)
        self.addCleanup(index2.close)
        root = share.import_car(store2, index2, car)[0]
        self.assertEqual(seats.manifest(store2, root), seats.manifest(self.store, sid))
        dest = os.path.join(self.tmp.name, "moved")
        written = seats.restore(store2, root, dest)
        for path, (name, digest, size) in zip(written, seats.manifest(store2, root)):
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual((hashlib.sha256(data).hexdigest(), len(data)), (digest, size))

    def test_a_snap_shows_its_manifest_and_never_the_bytes_of_a_database(self):
        from breadcrumbs.zoom import Zoom
        facts_db(os.path.join(self.home, "memory_store.db"), [("tea", "general")])
        sid = seats.snap(self.store, self.refs, "holographic", self.home)
        for level in (0.3, 1):
            text = Zoom(self.store).render(sid, level)
            self.assertNotIn("SQLite format", text)
            self.assertNotIn("\x00", text)
        self.assertIn("memory_store.db", text)
        self.assertRegex(text, r"\(binary, \d+ bytes: not shown\)")

    def test_nothing_to_snap_gives_none_and_no_commit(self):
        self.assertIsNone(seats.snap(self.store, self.refs, "holographic", self.home))
        self.assertIsNone(self.refs.get("seat-holographic"))


class HarvestTest(Base):
    def test_each_record_is_kept_once_with_its_system_and_its_own_id(self):
        facts_db(os.path.join(self.home, "memory_store.db"),
                 [("the kettle descales with citric acid", "general"), ("tea steeps four minutes", "user_pref")])
        got, new = seats.harvest(self.store, self.index, self.refs, "holographic", self.home)
        self.assertEqual((got, len(new)), (2, 2))
        metas = [get_crumb(self.store, c).meta for c in new]
        self.assertEqual([(m["src"], m["key"]) for m in metas], [("holographic", "1"), ("holographic", "2")])
        hits = self.index.search("citric", 5)
        self.assertEqual([h[0] for h in hits], [new[0]])
        self.assertEqual(get_text(self.store, get_crumb(self.store, new[0]).ranges[0][0]),
                         b"the kettle descales with citric acid")
        self.assertEqual(seats.harvest(self.store, self.index, self.refs, "holographic", self.home), (2, []))
        self.assertEqual(len(list(log(self.store, self.refs.get("seat-holographic")))), 1)  # no empty commit

    def test_the_hermes_memory_files_give_one_record_per_entry_with_its_target(self):
        self.write("memories/MEMORY.md", "The kettle is in the left cupboard.\n\u00a7\nTea steeps four minutes.")
        self.write("memories/USER.md", "Prefers green tea.")
        got, new = seats.harvest(self.store, self.index, self.refs, "builtin", self.home)
        self.assertEqual(got, 3)
        self.assertEqual([get_crumb(self.store, c).meta.get("target") for c in new], ["memory", "memory", "user"])
        self.assertEqual(get_text(self.store, get_crumb(self.store, new[1]).ranges[0][0]), b"Tea steeps four minutes.")

    def test_a_context_tree_gives_one_record_per_file_keyed_by_its_path(self):
        self.write("byterover/.brv/context-tree/tools/kettle.md", "# Kettle\nDescale monthly.\n")
        got, new = seats.harvest(self.store, self.index, self.refs, "byterover", self.home)
        self.assertEqual(got, 1)
        self.assertEqual(get_crumb(self.store, new[0]).meta["key"], ".brv/context-tree/tools/kettle.md")

    def test_a_system_without_local_data_gives_no_records(self):
        self.assertEqual(seats.harvest(self.store, self.index, self.refs, "mem0", self.home), (0, []))

    def test_the_cli_snaps_and_harvests_and_restores(self):
        facts_db(os.path.join(self.home, "memory_store.db"), [("the kettle descales with citric acid", "general")])
        self.close()  # the CLI opens the store itself
        for action in ("snap", "harvest"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--home", self.root, "seat", action, "holographic", self.home]), 0)
            self.assertIn("holographic", out.getvalue())
        dest = os.path.join(self.tmp.name, "moved")
        os.makedirs(dest)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):  # a seat name restores its newest snap
            self.assertEqual(cli.main(["--home", self.root, "seat", "restore", "holographic", dest]), 0)
        self.assertTrue(os.path.isfile(os.path.join(dest, "memory_store.db")))
        self.reopen()
        self.assertEqual(len(self.index.search("citric", 5)), 1)


class Recorder:
    """A memory provider that records what it is given."""

    def __init__(self, name):
        self.name, self.writes, self.calls = name, [], []

    def on_memory_write(self, action, target, content, metadata=None):
        self.writes.append((action, target, content))

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, args))
        return json.dumps({"result": "ok"})


class ReplayTest(Base):
    def setUp(self):
        super().setUp()
        self.write("memories/MEMORY.md", "The kettle is in the left cupboard.\n\u00a7\nTea steeps four minutes.")
        self.write("memories/USER.md", "Prefers green tea.")
        seats.harvest(self.store, self.index, self.refs, "builtin", self.home)

    def test_kept_records_go_to_another_system_through_its_memory_hook_with_their_target(self):
        dest = Recorder("supermemory")
        sent = seats.replay(self.store, self.refs, "seat-builtin", dest, "supermemory")
        self.assertEqual(dest.writes, [("add", "memory", "The kettle is in the left cupboard."),
                                       ("add", "memory", "Tea steeps four minutes."), ("add", "user", "Prefers green tea.")])
        head = get_crumb(self.store, self.refs.get("seat-supermemory"))
        self.assertEqual(head.kids, sent)  # what went where, under the receiving system's ref
        self.assertIn("seat-builtin", head.meta["msg"])

    def test_a_system_without_that_hook_gets_its_own_write_tool(self):
        dest = Recorder("mem0")
        seats.replay(self.store, self.refs, "seat-builtin", dest, "mem0")
        self.assertEqual(dest.calls[0], ("mem0_add", {"content": "The kettle is in the left cupboard."}))
        self.assertEqual(dest.writes, [])

    def test_a_system_gets_only_the_targets_that_it_keeps(self):
        dest = Recorder("honcho")  # honcho keeps facts about the user only
        sent = seats.replay(self.store, self.refs, "seat-builtin", dest, "honcho")
        self.assertEqual(dest.writes, [("add", "user", "Prefers green tea.")])
        self.assertEqual(len(sent), 1)

    def test_a_second_replay_sends_nothing_again(self):
        dest = Recorder("supermemory")
        self.assertEqual(len(seats.replay(self.store, self.refs, "seat-builtin", dest, "supermemory")), 3)
        self.assertEqual(seats.replay(self.store, self.refs, "seat-builtin", dest, "supermemory"), [])
        self.assertEqual(len(dest.writes), 3)

    def test_texts_never_go_back_to_the_system_they_came_from(self):
        facts_db(os.path.join(self.home, "memory_store.db"), [("tea steeps four minutes", "general")])
        seats.harvest(self.store, self.index, self.refs, "holographic", self.home)
        dest = Recorder("holographic")
        self.assertEqual(seats.replay(self.store, self.refs, "seat-holographic", dest, "holographic"), [])
        self.assertEqual(dest.writes, [])

    def test_a_failed_write_is_not_counted_as_sent(self):
        class Failing(Recorder):
            def handle_tool_call(self, tool_name, args, **kwargs):
                return json.dumps({"error": "quota"})
        self.assertEqual(seats.replay(self.store, self.refs, "seat-builtin", Failing("mem0"), "mem0"), [])
        self.assertIsNone(self.refs.get("seat-mem0"))

    def test_the_cli_replays_into_a_loaded_provider(self):
        dest = Recorder("supermemory")
        self.close()
        old = seats.load_provider
        seats.load_provider = lambda name, home: dest
        self.addCleanup(setattr, seats, "load_provider", old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", self.root, "seat", "replay", "supermemory", self.home, "--from", "seat-builtin"])
        self.assertEqual(code, 0)
        self.assertEqual(len(dest.writes), 3)
        self.assertIn("3", out.getvalue())


class SurveyTest(Base):
    def test_each_known_system_is_listed_with_its_way_and_where_it_is(self):
        os.makedirs(os.path.join(self.home, "plugins", "mem0"))
        self.write("plugins/mem0/__init__.py", PROVIDER)
        self.write("plugins/other/__init__.py", PROVIDER)  # a catalog provider that the table does not know
        self.write("plugins/bc/__init__.py", "from breadcrumbs.hermes import BreadcrumbsProvider\n")
        facts_db(os.path.join(self.home, "memory_store.db"), [("tea", "general")])
        rows = {r["name"]: r for r in seats.survey(self.home, repo=os.path.join(self.tmp.name, "no-repo"))}
        self.assertEqual(rows["mem0"]["way"], "reuse")
        self.assertIn("installed", rows["mem0"]["where"])
        self.assertEqual(rows["holographic"]["data"], 1)
        self.assertEqual(rows["other"]["way"], "reuse")
        self.assertNotIn("bc", rows)  # breadcrumbs itself is not a seat
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--home", self.root, "seats", self.home]), 0)
        self.assertIn("holographic", out.getvalue())


class QuietSlot:
    """A slot provider that recalls nothing and keeps state outside HERMES_HOME."""
    name = "quiet"

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def backup_paths(self):
        return ["~/.quiet-memory"]


class WithSlotTest(unittest.TestCase):
    def test_a_harvested_record_waits_under_a_running_slot(self):
        # THRU: while a slot runs, the recall is the slot's own. The record stays in the store, and
        # breadcrumbs recalls it when it runs alone again (slot --off).
        from unittest import mock
        from breadcrumbs import slot as slot_mod
        from breadcrumbs.hermes import BreadcrumbsProvider
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "breadcrumbs"))
        config = os.path.join(tmp.name, "breadcrumbs", "config.json")
        with open(config, "w") as f:
            json.dump({"slot": "quiet"}, f)
        p = BreadcrumbsProvider()
        with mock.patch.object(slot_mod, "load", lambda name: QuietSlot() if name == "quiet" else None):
            p.initialize("s1", hermes_home=tmp.name, platform="cli", agent_context="primary")
        self.assertIsNotNone(p.slot)
        facts_db(os.path.join(tmp.name, "memory_store.db"), [("the walrus kettle descales with citric acid", "general")])
        _got, new = seats.harvest(p.store, p.index, p.refs, "holographic", tmp.name)
        self.assertEqual(p.prefetch("how does the walrus kettle descale?", session_id="s1"), "")
        p.shutdown()
        with open(config, "w") as f:
            json.dump({}, f)
        alone = BreadcrumbsProvider()
        alone.initialize("s2", hermes_home=tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(alone.shutdown)
        self.assertIn(new[0], alone.prefetch("how does the walrus kettle descale?", session_id="s2"))

    def test_the_memory_that_breadcrumbs_mirrors_replays_with_its_target_and_without_removals(self):
        from breadcrumbs.hermes import BreadcrumbsProvider
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        p.on_memory_write("add", "user", "Prefers green tea.")
        p.on_memory_write("remove", "memory", "", metadata={"previous_content": "The kettle is blue."})
        dest = Recorder("supermemory")
        sent = seats.replay(p.store, p.refs, "memory", dest, "supermemory")
        self.assertEqual(dest.writes, [("add", "user", "Prefers green tea.")])
        self.assertEqual(len(sent), 1)

    def test_hermes_backup_keeps_the_state_of_the_slot_before_and_after_initialize(self):
        from unittest import mock
        from breadcrumbs import slot as slot_mod
        from breadcrumbs.hermes import BreadcrumbsProvider
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "breadcrumbs"))
        with open(os.path.join(tmp.name, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"slot": "quiet"}, f)
        load = mock.patch.object(slot_mod, "load", lambda name: QuietSlot() if name == "quiet" else None)
        with load, mock.patch.dict(os.environ, {"HERMES_HOME": tmp.name}):
            fresh = BreadcrumbsProvider()  # Hermes's backup loads the active provider and asks it at once
            self.assertEqual(fresh.backup_paths(), ["~/.quiet-memory"])
            p = BreadcrumbsProvider()
            p.initialize("s1", hermes_home=tmp.name, platform="cli", agent_context="primary")
            self.addCleanup(p.shutdown)
            self.assertEqual(p.backup_paths(), ["~/.quiet-memory"])


if __name__ == "__main__":
    unittest.main()
