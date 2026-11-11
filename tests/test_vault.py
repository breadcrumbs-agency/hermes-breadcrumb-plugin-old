"""vault: an Obsidian vault from a store (export) and crumbs from a vault (import), with exact bytes."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, vault, view
from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.hermes import BreadcrumbsProvider, _ref_name
from breadcrumbs.history import log
from breadcrumbs.ingest import leaves
from breadcrumbs.lines import get_lines, put_text
from breadcrumbs.share import publish

SESSIONS = {  # oldest first: the clock gives each turn a later time
    "tea1": [("how long should green tea steep in the kettle water", "steep green tea two minutes; kettle water near 80 C")],
    "tea2": [("the kettle boils too fast for black tea", "black tea likes the kettle water just off the boil")],
    "bike": [("my bike chain skips on the small cog", "replace the worn chain and check every cog for wear")],
    "cafe": [("Café crème: the kettle [draft] #1 | plan?",
              "Heat the kettle to 90 C for café crème. The [[draft]] note has `code` and a ``` fence.")],
}
T0 = 1_794_000_000


def front(text):
    """The frontmatter of a note: {key: value}; a list for a key with "  - " items."""
    assert text.startswith("---\n"), text[:40]
    out, key = {}, None
    for line in text[4:].split("\n---\n", 1)[0].split("\n"):
        if line.startswith("  - "):
            out[key].append(json.loads(line[4:]))
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        out[key] = json.loads(value) if value[:1] in ('"', "[") else (value or [])
    return out


def notes(root):
    """{vault path: bytes} of each .md file under root."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".md"):
                path = os.path.join(base, name)
                with open(path, "rb") as f:
                    out[os.path.relpath(path, root).replace(os.sep, "/")] = f.read()
    return out


def tree(root):
    """{path: bytes} of each file under root."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            with open(os.path.join(base, name), "rb") as f:
                out[os.path.relpath(os.path.join(base, name), root)] = f.read()
    return out


def text_of(store, crumb_id):
    c = get_crumb(store, crumb_id)
    return b"".join(b"".join(get_lines(store, doc, a, b)) for doc, a, b in c.ranges)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after each View closes (Windows)
        self.p = BreadcrumbsProvider()
        self.p.initialize("tea1", hermes_home=self.tmp.name, platform="cli", agent_context="primary")
        self.addCleanup(self.p.shutdown)
        tick = iter(range(T0, T0 + 10 ** 6, 37))
        self.p.clock = lambda: next(tick)
        for sid, turns in SESSIONS.items():
            for u, a in turns:
                self.p.sync_turn(u, a, session_id=sid)
        self.sess = {sid: self.p.session_crumb(sid) for sid in SESSIONS}
        self.root = os.path.join(self.tmp.name, "breadcrumbs")

    def path(self, *parts):
        return os.path.join(self.tmp.name, *parts)

    def open(self, root=None):
        v = view.View(root or self.root)
        self.addCleanup(v.close)
        return v

    def export(self, out, everything=True):
        self.p.shutdown()
        return vault.export_vault(self.open(), out, everything=everything)

    def by_crumb(self, files):
        """{session crumb id: (vault path, text)} of the session notes."""
        out = {}
        for rel, data in files.items():
            if rel.startswith("sessions/"):
                text = data.decode("utf-8")
                out[front(text)["crumb"]] = (rel, text)
        return out


class ExportTest(StoreCase):
    def test_one_note_per_session_with_its_exact_turns_and_their_crumb_ids(self):
        out = self.path("vault")
        stats = self.export(out)
        files = notes(out)
        self.assertEqual(stats["sessions"], 4)
        sessions = self.by_crumb(files)
        self.assertEqual(set(sessions), set(self.sess.values()))
        v = self.open()
        for cid in self.sess.values():
            rel, text = sessions[cid]
            meta = front(text)
            self.assertEqual(meta["tags"], v.zoom.cue_words(cid).split())  # the cue words of the session handle
            self.assertRegex(meta["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")  # an Obsidian date & time
            self.assertEqual(meta["turns"], "2")
            for turn in leaves(v.store, cid):
                self.assertIn("`%s`" % turn, text)  # each turn names its crumb id
                self.assertIn(text_of(v.store, turn), files[rel])  # and holds its exact bytes
        self.assertEqual(sessions[self.sess["cafe"]][0], "sessions/Café crème the kettle draft 1 plan.md")
        self.assertEqual(sessions[self.sess["tea1"]][0], "sessions/how long should green tea steep in the kettle water.md")

    def test_markdown_in_a_turn_makes_no_link_and_breaks_no_fence(self):
        out = self.path("vault")
        self.export(out)
        _rel, text = self.by_crumb(notes(out))[self.sess["cafe"]]
        self.assertIn("````text\n", text)  # the fence is longer than the ``` in the turn
        self.assertNotIn("draft", vault.wikilinks(text))  # "[[draft]]" is inside the fence

    def test_two_sessions_with_one_title_get_two_names(self):
        self.p.sync_turn(*SESSIONS["tea2"][0], session_id="tea3")
        out = self.path("vault")
        self.export(out)
        names = sorted(rel for rel in notes(out) if rel.startswith("sessions/the kettle boils"))
        self.assertEqual(len(names), 2)
        for rel in names:
            self.assertRegex(rel, r"^sessions/the kettle boils too fast for black tea \([0-9a-f]{8}(\.\d+)?\)\.md$")

    def test_a_folder_with_other_files_is_not_written(self):
        other = self.path("other")
        os.makedirs(other)
        with open(os.path.join(other, "mine.md"), "w", encoding="utf-8") as f:
            f.write("my own note\n")
        with self.assertRaises(FileExistsError):
            self.export(other)
        self.assertEqual(os.listdir(other), ["mine.md"])


class LinkTest(StoreCase):
    def sessions_by_path(self, files):
        return {rel[:-3]: cid for cid, (rel, _text) in self.by_crumb(files).items()}

    def test_sessions_that_share_cue_words_link_to_each_other_as_in_the_graph(self):
        refs = os.path.join(self.root, "refs")
        for i, sid in enumerate(SESSIONS):  # the graph orders by ref time: here the order of the turn times
            os.utime(os.path.join(refs, _ref_name("sess-", sid)), (1000 + i, 1000 + i))
        out = self.path("vault")
        self.export(out)
        files = notes(out)
        crumb = self.sessions_by_path(files)
        got = set()
        for cid, (_rel, text) in self.by_crumb(files).items():
            for target in vault.wikilinks(text):
                got.add(frozenset((cid, crumb[target])))
        edges = self.open().graph()["edges"]
        self.assertEqual(got, {frozenset((e["source"], e["target"])) for e in edges})
        self.assertIn(frozenset((self.sess["tea1"], self.sess["tea2"])), got)

    def test_the_index_links_to_every_session_newest_first_and_to_every_hub(self):
        out = self.path("vault")
        stats = self.export(out)
        files = notes(out)
        links = vault.wikilinks(files["index.md"].decode("utf-8"))
        sessions = [t for t in links if t.startswith("sessions/")]
        newest = [self.sess[s] for s in reversed(list(SESSIONS))]
        self.assertEqual([self.sessions_by_path(files)[t] for t in sessions], newest)
        hubs = sorted(rel[:-3] for rel in files if rel.startswith("cues/"))
        self.assertEqual(sorted(t for t in links if t.startswith("cues/")), hubs)
        self.assertEqual(len(links), len(sessions) + len(hubs))
        self.assertEqual((stats["sessions"], stats["cues"], stats["notes"]), (4, len(hubs), 4 + len(hubs) + 1))

    def test_one_hub_note_per_cue_word_links_to_its_sessions_newest_first(self):
        out = self.path("vault")
        self.export(out)
        files = notes(out)
        crumb = self.sessions_by_path(files)
        v = self.open()
        cues = {cid: [w.lower() for w in v.zoom.cue_words(cid).split()] for cid in self.sess.values()}
        words = sorted({w for ws in cues.values() for w in ws})
        self.assertEqual(sorted(rel for rel in files if rel.startswith("cues/")), ["cues/%s.md" % w for w in words])
        newest = [self.sess[s] for s in reversed(list(SESSIONS))]
        for w in words:
            text = files["cues/%s.md" % w].decode("utf-8")
            self.assertEqual([crumb[t] for t in vault.wikilinks(text)], [c for c in newest if w in cues[c]])
            self.assertEqual(front(text)["cue"], w)
        self.assertEqual(len(vault.wikilinks(files["cues/tea.md"].decode("utf-8"))), 2)  # tea1 and tea2

    def test_a_summary_is_marked_as_model_text_with_its_source_ids(self):
        p, cid = self.p, self.sess["tea1"]
        text = b"Green tea wants kettle water near 80 C.\n"
        doc, prompt = put_text(p.store, text), put_text(p.store, b"the model input")
        sid = put_crumb(p.store, Crumb("sum", "summary: Green tea wants kettle water near 80 C.", ranges=[[doc, 0, 1]],
                                       heritage=[cid], meta={"by": "llama", "model": "small-model.gguf",
                                                             "model_sha256": "ab" * 32, "input": prompt}))
        p.index.add(sid, "%s:0-1" % doc, text.decode(), kind="sum")
        p.index.set_summary(cid, sid)
        p.store.flush()
        p.index.commit()
        out = self.path("vault")
        self.export(out)
        sessions = self.by_crumb(notes(out))
        note = sessions[cid][1]
        self.assertEqual(front(note)["summary"], sid)
        top = note.split("## Turns")[0]
        self.assertIn("> [!summary] Written by a model (small-model.gguf), not by a person\n"
                      "> Green tea wants kettle water near 80 C.\n", top)
        for source in (sid, cid, prompt, "ab" * 32):
            self.assertIn("`%s`" % source, top)
        self.assertNotIn("[!summary]", sessions[self.sess["tea2"]][1])

    def test_private_crumbs_stay_out_unless_all(self):
        refs, store = self.p.refs, self.p.store
        pub = publish(store, self.sess["bike"], deep=True)  # a public session, each turn public
        refs.set(_ref_name("sess-", "bike"), pub)
        half = publish(store, self.sess["tea2"])  # a public session over private turns
        refs.set(_ref_name("sess-", "tea2"), half)
        store.flush()
        out = self.path("public")
        stats = self.export(out, everything=False)
        files = notes(out)
        sessions = self.by_crumb(files)
        self.assertEqual((set(sessions), stats["sessions"]), ({pub, half}, 2))
        blob = b"".join(files.values())
        self.assertIn(b"replace the worn chain", blob)
        for private in (b"just off the boil", b"green tea", "café".encode()):
            self.assertNotIn(private, blob)  # the text of a private turn
        self.assertNotIn("cues/tea.md", files)  # a cue word of a private text
        self.assertEqual((front(sessions[half][1])["turns"], front(sessions[half][1])["tags"]), ("0", []))
        everything = self.path("all")
        vault.export_vault(self.open(), everything, everything=True)
        self.assertEqual(len(self.by_crumb(notes(everything))), 4)

    def test_the_same_store_gives_the_same_bytes(self):
        a, b, c = self.path("a"), self.path("b"), self.path("c")
        self.export(a)
        self.assertEqual(vault.export_vault(self.open(), b, everything=True)["written"], len(notes(a)))
        self.assertEqual(vault.export_vault(self.open(), b, everything=True)["written"], 0)  # nothing changed
        copy = self.path("copy")
        shutil.copytree(self.root, copy, copy_function=shutil.copy)  # new file times
        for i, name in enumerate(sorted(os.listdir(os.path.join(copy, "refs")), reverse=True)):
            os.utime(os.path.join(copy, "refs", name), (5000 + i, 5000 + i))  # another ref order by time
        vault.export_vault(self.open(copy), c, everything=True)
        self.assertEqual(tree(a), tree(b))
        self.assertEqual(tree(a), tree(c))

    def test_a_second_export_removes_only_the_notes_of_the_first(self):
        out = self.path("vault")
        self.export(out)
        os.makedirs(os.path.join(out, ".obsidian"))
        with open(os.path.join(out, ".obsidian", "app.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(out, vault.MANIFEST), "a", encoding="utf-8") as f:
            f.write("sessions/gone.md\n../outside.md\n")
        with open(os.path.join(out, "sessions", "gone.md"), "w", encoding="utf-8") as f:
            f.write("an old note\n")
        with open(self.path("outside.md"), "w", encoding="utf-8") as f:
            f.write("not in the vault\n")
        self.assertEqual(vault.export_vault(self.open(), out, everything=True)["removed"], 1)
        self.assertFalse(os.path.exists(os.path.join(out, "sessions", "gone.md")))
        self.assertTrue(os.path.exists(os.path.join(out, ".obsidian", "app.json")))
        self.assertTrue(os.path.exists(self.path("outside.md")))


class ImportTest(StoreCase):
    VAULT = {  # a hand-made vault: unicode and spaces in names, CRLF, no last newline, an empty note
        "A.md": b"---\ntags: [tea]\n---\n# A\nSee [[B]], [[Caf\xc3\xa9 notes/Tea & kettle|the tea note]] and [[B#Steps]].\n"
                b"Not here: [[missing]], ![[image.png]], [[A]], `[[B in code]]`.\n```\n[[Tea & kettle]]\n```\n",
        "sub/B.md": b"# B\r\n\r\nSteps: boil, wait, pour.\r\nBack to [[A|the start]]",
        "Caf\u00e9 notes/Tea & kettle.md": "Tea & kettle: \u00e9\u00e8 \U0001f375\n".encode() + b"word " * 120 + b"\n",
        "empty.md": b"",
        ".obsidian/workspace.md": b"not a note: Obsidian keeps its settings here\n",
        "image.png": b"\x89PNG\r\n",
    }

    def write_vault(self, name="hand", files=None):
        root = self.path(name)
        for rel, data in (files or self.VAULT).items():
            path = os.path.join(root, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        return root

    def new_store(self, name="new"):
        home = self.path(name)
        store, index, refs, _zoom = cli.open_all(home)
        self.addCleanup(store.close)
        self.addCleanup(index.close)
        return store, index, refs, home

    def import_(self, src, name="new", ts=T0):
        store, index, refs, home = self.new_store(name) if not hasattr(self, "dest_" + name) else getattr(self, "dest_" + name)
        setattr(self, "dest_" + name, (store, index, refs, home))
        return vault.import_vault(store, index, refs, src, ts=ts), store, index, refs, home

    def notes_of(self, store, root):
        """{vault path: note crumb id} of the vault crumb root."""
        return {get_crumb(store, k).meta["path"]: k for k in get_crumb(store, root).kids}

    def assert_exact(self, home, crumbs, files):
        """At level 1 the text range of each note crumb gives the exact file bytes; the crumb gives its
        handle line and then the same bytes."""
        v = self.open(home)
        for rel, cid in crumbs.items():
            (doc, a, b), = get_crumb(v.store, cid).ranges
            self.assertEqual(v.crumb("%s:%d-%d" % (doc, a, b), 1.0)["text"].encode("utf-8"), files[rel], rel)
            head, _, body = v.crumb(cid, 1.0)["text"].partition("\n")
            self.assertEqual(head.split()[:3], ["\u25b8", cid, "note"], rel)
            self.assertIn(body.encode("utf-8"), (files[rel], files[rel] + b"\n"), rel)  # a last line gets its newline

    def test_each_note_becomes_a_crumb_that_opens_to_its_exact_bytes(self):
        src = self.write_vault()
        stats, store, _index, _refs, home = self.import_(src)
        crumbs = self.notes_of(store, stats["root"])
        notes_ = {rel: data for rel, data in self.VAULT.items() if rel.endswith(".md") and not rel.startswith(".")}
        self.assertEqual(sorted(crumbs), sorted(notes_))
        self.assertEqual((stats["notes"], stats["added"]), (4, 4))
        store.flush()
        self.assert_exact(home, crumbs, notes_)
        self.assertEqual(get_crumb(store, crumbs["Caf\u00e9 notes/Tea & kettle.md"]).title, "Tea & kettle")

    def test_each_wikilink_becomes_a_link_between_the_crumbs_of_the_two_notes(self):
        stats, store, index, _refs, _home = self.import_(self.write_vault())
        c = self.notes_of(store, stats["root"])
        a, b, t = c["A.md"], c["sub/B.md"], c["Caf\u00e9 notes/Tea & kettle.md"]
        self.assertEqual(get_crumb(store, stats["root"]).meta["links"], sorted([[a, b], [a, t], [b, a]]))
        self.assertEqual((stats["links"], stats["unresolved"]), (3, 2))  # missing and image.png
        pairs = set(index.db.execute("SELECT a, b FROM nbr").fetchall())
        self.assertEqual(pairs, {(a, b), (b, a), (a, t), (t, a)})  # recall finds the linked note too

    def test_a_second_import_of_an_unchanged_vault_adds_nothing(self):
        src = self.write_vault()
        first, store, index, refs, _home = self.import_(src)
        count = (len(store), index.db.execute("SELECT count(*) FROM rows").fetchone(), sorted(refs.names()),
                 {n: refs.get(n) for n in refs.names()})
        second = self.import_(src, ts=T0 + 60)[0]
        self.assertEqual((second["added"], second["root"]), (0, first["root"]))
        self.assertEqual(count, (len(store), index.db.execute("SELECT count(*) FROM rows").fetchone(),
                                 sorted(refs.names()), {n: refs.get(n) for n in refs.names()}))

    def test_a_changed_note_adds_a_new_crumb_and_keeps_the_old_one(self):
        src = self.write_vault()
        first, store, _index, refs, home = self.import_(src)
        with open(os.path.join(src, "sub", "B.md"), "ab") as f:
            f.write(b"\r\nOne more line.")
        second = self.import_(src, ts=T0 + 60)[0]
        self.assertEqual(second["added"], 1)
        old, new = self.notes_of(store, first["root"]), self.notes_of(store, second["root"])
        self.assertNotEqual(old["sub/B.md"], new["sub/B.md"])
        self.assertEqual({k: v for k, v in old.items() if k != "sub/B.md"}, {k: v for k, v in new.items() if k != "sub/B.md"})
        store.flush()
        self.assert_exact(home, {"sub/B.md": old["sub/B.md"]}, self.VAULT)  # the old crumb stays
        self.assertEqual([get_crumb(store, cid).kids for cid, _c in log(store, refs.get("main"))],
                         [[second["root"]], [first["root"]]])  # the history holds both

    def test_the_same_vault_gives_the_same_crumbs_in_each_store(self):
        src = self.write_vault()
        self.assertEqual(self.import_(src, "one")[0]["root"], self.import_(src, "two", ts=T0 + 999)[0]["root"])

    def test_round_trip_export_then_import(self):
        out = self.path("vault")
        self.export(out)
        files = notes(out)
        stats, store, _index, _refs, home = self.import_(out)
        crumbs = self.notes_of(store, stats["root"])
        self.assertEqual(sorted(crumbs), sorted(files))
        store.flush()
        self.assert_exact(home, crumbs, files)
        path_of = {cid: rel for rel, cid in crumbs.items()}
        got = {(path_of[a], path_of[b]) for a, b in get_crumb(store, stats["root"]).meta["links"]}
        want = {(rel, t + ".md") for rel, data in files.items() for t in vault.wikilinks(data.decode("utf-8"))}
        self.assertEqual(got, want)
        self.assertEqual(stats["unresolved"], 0)


class NameTest(unittest.TestCase):
    def test_a_name_holds_no_character_that_breaks_a_file_name_or_a_link(self):
        self.assertEqual(vault.safe_name("Café crème: the kettle [draft] #1 | plan?"), "Café crème the kettle draft 1 plan")
        self.assertEqual(vault.safe_name('a/b\\c*d?e"f<g>h^i'), "a b c d e f g h i")
        self.assertEqual(vault.safe_name("..hidden name. "), "hidden name")
        self.assertEqual(vault.safe_name("tab\there\nnew line"), "tab here new line")
        self.assertEqual(vault.safe_name("Cafe\u0301"), "Caf\u00e9")  # NFC
        self.assertEqual(vault.safe_name("\U0001f375" * 70), "\U0001f375" * 50)  # at most 200 bytes
        self.assertEqual(vault.safe_name("[]#"), "untitled")

    def test_a_shared_or_reserved_name_gets_the_crumb_id(self):
        self.assertEqual(vault.unique_names([("a1", "con"), ("b2", "index"), ("c3", "Plan"), ("d4", "plan")]),
                         {"a1": "con (a1)", "b2": "index", "c3": "Plan (c3)", "d4": "plan (d4)"})


class CliTest(StoreCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_vault_export_through_the_cli(self):
        self.p.shutdown()
        out = self.path("vault")
        code, got = self.run_cli("--home", self.root, "vault", "export", out)
        self.assertEqual((code, json.loads(got)["sessions"]), (0, 0))  # every crumb is private
        code, got = self.run_cli("--home", self.root, "vault", "export", out, "--all")
        self.assertEqual((code, json.loads(got)["sessions"]), (0, 4))
        self.assertEqual(len([r for r in notes(out) if r.startswith("sessions/")]), 4)

    def test_vault_import_through_the_cli(self):
        self.p.shutdown()
        out, home = self.path("vault"), self.path("new")
        self.run_cli("--home", self.root, "vault", "export", out, "--all")
        code, got = self.run_cli("--home", home, "vault", "import", out)
        stats = json.loads(got)
        self.assertEqual((code, stats["notes"], stats["added"]), (0, len(notes(out)), len(notes(out))))
        code, got = self.run_cli("--home", home, "vault", "import", out)
        self.assertEqual((code, json.loads(got)["added"]), (0, 0))
        v = self.open(home)
        kids = {get_crumb(v.store, k).meta["path"]: k for k in get_crumb(v.store, stats["root"]).kids}
        (doc, a, b), = get_crumb(v.store, kids["index.md"]).ranges
        code, got = self.run_cli("--home", home, "bc", "%s:%d-%d" % (doc, a, b), "1")
        self.assertEqual((code, got.encode("utf-8")), (0, notes(out)["index.md"]))


if __name__ == "__main__":
    unittest.main()
