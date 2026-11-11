import sqlite3
import os
import tempfile
import unittest

from breadcrumbs.index import HOLDER, Index


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/index.db"
        self.ix = Index(self.path)
        self.ix.add("aaaaaaaa", "d1:0-3", "the parser crashed on an empty line in lines.py")
        self.ix.add("bbbbbbbb", "d2:0-2", "graalpy runs the tests faster after warmup")
        self.ix.add("cccccccc", "d3:0-4", "the user prefers short answers in plain english")

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def test_rare_word_finds_its_crumb_first(self):
        hits = self.ix.search("graalpy")
        self.assertEqual(hits[0][0], "bbbbbbbb")
        self.assertEqual(hits[0][1], "d2:0-2")

    def test_more_matching_terms_rank_higher(self):
        hits = self.ix.search("parser empty line")
        self.assertEqual(hits[0][0], "aaaaaaaa")

    def test_punctuation_in_a_query_is_safe(self):
        self.assertEqual(self.ix.search('what "did" (the) user* prefer? -NOT'), self.ix.search("what did the user prefer not"))
        self.assertEqual(self.ix.search("?!"), [])

    def test_index_survives_reopen(self):
        self.ix.close()
        self.ix = Index(self.path)
        self.assertEqual(self.ix.search("english")[0][0], "cccccccc")

    def test_same_crumb_range_is_indexed_once(self):
        self.ix.add("aaaaaaaa", "d1:0-3", "the parser crashed on an empty line in lines.py")
        self.assertEqual(len(self.ix.search("parser")), 1)


class SignatureTest(unittest.TestCase):
    """Cue words: the words a text uses more than the rest of the index (Dunning's G2 over stems)."""

    TEXT = ("Kettle notes: the kettle boils water. Steeping tea in the kettle takes four minutes; "
            "steeping longer makes tea bitter.")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ix = Index(self.tmp.name + "/index.db")
        rest = ["the parser crashed on an empty line; the parser reads every line twice",
                "graalpy runs the tests faster after warmup, and the tests pass",
                "the user prefers short answers; short answers in plain english",
                "every line of the report takes minutes to read"]
        for i, text in enumerate(rest):
            self.ix.add("r%07d" % i, "d%d:0-1" % i, text)
        self.ix.add("t0000000", "dt:0-1", self.TEXT)

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def test_the_most_used_words_come_in_order_of_first_use(self):
        self.assertEqual(self.ix.signature(self.TEXT, 3), ["kettle", "steeping", "tea"])

    def test_words_the_rest_also_uses_rank_lower(self):
        # "takes" and "minutes" come before "longer" in the text, but the rest of the index uses them
        self.assertEqual(self.ix.signature(self.TEXT, 8),
                         ["kettle", "notes", "boils", "water", "steeping", "tea", "four", "longer"])

    def test_a_word_shows_as_written_and_lowercase_when_the_text_has_both(self):
        self.ix.add("t0000001", "du:0-1", "Brindle reads; Brindle sleeps; brindle brindle.")
        self.ix.add("t0000002", "dv:0-1", "Ostrava Ostrava Ostrava is far.")
        self.assertEqual(self.ix.signature("Brindle reads; Brindle sleeps; brindle brindle.", 1), ["brindle"])
        self.assertEqual(self.ix.signature("Ostrava Ostrava Ostrava is far.", 1), ["Ostrava"])

    def test_stop_words_numbers_short_words_and_hex_ids_are_not_cues(self):
        self.assertEqual(self.ix.signature("c0ffee12 c0ffee12 c0ffee12 2026 2026 ok ok would would would kettle"),
                         ["kettle"])

    def test_escapes_short_hex_ids_and_word_tails_are_not_cues(self):
        text = "\\nkettle \\nkettle d01f6 d01f6 b05 b05 4bit 4bit tea"
        self.ix.add("t0000005", "dz:0-1", text)
        self.assertEqual(self.ix.signature(text), ["kettle", "tea"])

    def test_a_word_the_text_uses_less_than_the_rest_is_not_a_cue(self):
        self.ix.add("r0000009", "dx:0-1", "line by line, line after line, every line")
        text = self.TEXT + " One line."
        self.ix.add("t0000003", "dw:0-1", text)
        cues = self.ix.signature(text, 30)
        self.assertIn("bitter", cues)
        self.assertNotIn("line", cues)

    def test_a_reader_sees_the_counts_of_new_writes(self):
        # The View reads index.db through its own connection while the provider writes to it. Cached counts
        # must not hide those writes: once the rest of the index uses "kettle" a lot, it is no cue.
        self.ix.commit()
        reader = Index(self.tmp.name + "/index.db", readonly=True)
        try:
            self.assertEqual(reader.signature(self.TEXT, 1), ["kettle"])
            for i in range(40):
                self.ix.add("k%07d" % i, "dk%d:0-1" % i, "kettle kettle kettle kettle kettle kettle")
            self.ix.commit()
            self.assertNotIn("kettle", reader.signature(self.TEXT, 3))
            self.assertNotIn("kettle", self.ix.signature(self.TEXT, 3))  # and the writer itself
        finally:
            reader.close()

    def test_forms_of_one_stem_count_together(self):
        text = "boils, boiling, boil. kettle kettle."
        self.ix.add("t0000004", "dy:0-1", text)
        self.assertEqual(self.ix.signature(text, 1), ["boils"])

    def test_a_long_text_in_a_small_index_has_cues(self):
        small = Index(":memory:")
        small.add("t0000000", "dt:0-1", self.TEXT)
        small.add("r0000000", "d0:0-1", "tea time")
        self.assertEqual(small.signature(self.TEXT, 2), ["kettle", "steeping"])
        small.close()

    def test_no_words_or_no_rest_gives_no_cues(self):
        self.assertEqual(self.ix.signature(""), [])
        self.assertEqual(self.ix.signature("the and of"), [])
        empty = Index(":memory:")
        self.assertEqual(empty.signature("kettle kettle tea"), [])
        empty.close()


class SalienceTest(unittest.TestCase):
    """Line weights for the zoom: each distinct stem of a line adds its log odds (the text against the
    rest of the index), clipped at 0."""

    TEXT = ("Kettle notes: the kettle boils water.\n"
            "Steeping tea in the kettle takes four minutes.\n"
            "the parser\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ix = Index(self.tmp.name + "/index.db")
        rest = ["the parser crashed on an empty line; the parser reads every line twice",
                "graalpy runs the tests faster after warmup, and the tests pass",
                "the user prefers short answers; short answers in plain english",
                "every line of the report takes minutes to read"]
        for i, text in enumerate(rest):
            self.ix.add("r%07d" % i, "d%d:0-1" % i, text)
        self.ix.add("t0000000", "dt:0-3", self.TEXT)
        self.mass = self.ix.salience(self.TEXT)

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def test_a_line_of_topic_words_outweighs_a_line_of_common_words(self):
        m = self.mass(self.TEXT.encode().splitlines(keepends=True))
        self.assertGreater(m[0], m[2])
        self.assertGreater(m[1], m[2])

    def test_words_the_rest_uses_more_weigh_nothing(self):
        self.assertEqual(self.mass([b"the parser\n", b"\n", b""]), [0.0, 0.0, 0.0])

    def test_each_stem_counts_once_per_line(self):
        self.assertEqual(self.mass([b"kettle kettle kettle\n"]), self.mass([b"kettle\n"]))
        self.assertEqual(self.mass([b"steeping steep\n"]), self.mass([b"steep\n"]))

    def test_a_rarer_topic_word_weighs_more(self):
        kettle, tea = self.mass([b"kettle\n", b"tea\n"])
        self.assertGreater(kettle, tea)
        self.assertGreater(tea, 0.0)


class NeighborTest(unittest.TestCase):
    """Dialogue-neighbor smoothing: s'(i) = s(i) + nb * max(s(prev), s(next))."""

    def setUp(self):
        self.ix = Index(":memory:")
        self.ix.add("q1", "d1:0-1", "where do the load tests run")
        self.ix.add("a1", "d2:0-1", "on the build host every night")  # no query word
        self.ix.add("x1", "d3:0-1", "unrelated chatter about lunch")
        self.ix.link("q1", "a1")
        self.ix.link("a1", "x1")

    def tearDown(self):
        self.ix.close()

    def test_plain_search_finds_only_the_matching_turn(self):
        self.assertEqual([c for c, _r, _s in self.ix.search("load tests")], ["q1"])

    def test_smoothing_lifts_the_reply_with_its_own_ref(self):
        hits = self.ix.search("load tests", nb=0.3)
        self.assertEqual([c for c, _r, _s in hits], ["q1", "a1"])
        self.assertEqual(hits[1][1], "d2:0-1")
        self.assertAlmostEqual(hits[1][2], 0.3 * hits[0][2])

    def test_two_steps_away_is_not_lifted(self):
        self.assertNotIn("x1", [c for c, _r, _s in self.ix.search("load tests", nb=0.3)])

    def test_link_is_idempotent(self):
        self.ix.link("q1", "a1")
        self.assertEqual(len(self.ix.search("load tests", nb=0.3)), 2)


class LockTest(unittest.TestCase):
    """Cue words and line weights only read the index. They leave no transaction open, so a reader
    never blocks the provider's writes ("database is locked"). A read-only index never writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + "/index.db"
        self.ix = Index(self.path)
        self.ix.add("r0000000", "d0:0-1", "the kettle boils water for green tea")
        self.ix.add("r0000001", "d1:0-1", "the bike chain skips on the small cog")
        self.ix.commit()

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def test_cue_words_and_line_weights_leave_no_transaction_open(self):
        self.assertTrue(self.ix.signature("the kettle boils water for green tea"))
        self.ix.salience("the kettle boils water")([b"kettle boils\n"])
        self.assertFalse(self.ix.db.in_transaction)

    def test_a_reader_with_cue_words_does_not_block_a_writer(self):
        ro = Index(self.path, readonly=True)
        try:
            ro.signature("the kettle boils water for green tea")
            ro.salience("the kettle boils water")([b"kettle boils\n"])
            self.ix.db.execute("PRAGMA busy_timeout = 200")
            self.ix.add("r0000002", "d2:0-1", "a third text about tea")
            self.ix.commit()
        finally:
            ro.close()

    def test_a_read_only_index_reads_and_never_writes(self):
        ro = Index(self.path, readonly=True)
        try:
            self.assertEqual([c for c, _, _ in ro.search("kettle")], ["r0000000"])
            self.assertFalse(ro.db.in_transaction)
            with self.assertRaises(sqlite3.OperationalError):
                ro.add("r0000002", "d2:0-1", "new text")
        finally:
            ro.close()


class TierTest(unittest.TestCase):
    """Word tiers for the zoom below level 1: distinct, meaningful, grammar.
    The first rule that hits decides: a stop word is grammar; a stem that this text uses more than
    the rest of the index is distinct (score: its log odds); a stem in many rows of the index is
    grammar; any other word is meaningful (score: its idf)."""

    TEXT = "The kettle boils water slowly, and the kettle hums.\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ix = Index(self.tmp.name + "/index.db")
        for i in range(30):  # "every" is in all rows; "slowly" 8 times in each of 2 rows; "parser" in one
            words = "every row says hello" + (" water" if i % 3 == 0 else "") + (" parser" if i == 0 else "")
            self.ix.add("r%07d" % i, "d%d:0-1" % i, words + (" slowly" * 8 if i in (1, 2) else ""))
        self.ix.add("t0000000", "dt:0-1", self.TEXT + "every kettle\n")
        self.ix.commit()
        self.classify = self.ix.tiers(self.TEXT + "every kettle\n")

    def tearDown(self):
        self.ix.close()
        self.tmp.cleanup()

    def test_each_rule_decides_its_tier(self):
        got = self.classify(["the", "kettle", "every", "slowly", "zebra"])
        self.assertEqual(got["the"][0], 2)      # stop word
        self.assertEqual(got["kettle"][0], 0)   # used here more than in the rest
        self.assertEqual(got["every"][0], 2)    # in every row of the index
        self.assertEqual(got["slowly"][0], 1)   # the rest uses it more, in 3 rows only: meaningful
        self.assertEqual(got["zebra"][0], 1)    # not in the index

    def test_scores_order_words_inside_a_tier(self):
        got = self.classify(["kettle", "boils", "slowly", "parser"])
        self.assertGreater(got["kettle"][1], got["boils"][1])  # twice here, never in the rest
        self.assertGreater(got["parser"][1], 0.0)

    def test_stems_decide_not_surface_forms(self):
        got = self.classify(["boils", "boil", "boiling"])
        self.assertEqual(got["boil"], got["boils"])
        self.assertEqual(got["boiling"], got["boils"])

    def test_tiers_leave_no_transaction_open(self):
        self.classify(["kettle", "every"])
        self.assertFalse(self.ix.db.in_transaction)

    def test_function_words_are_grammar_in_a_small_store(self):
        """A new store has few rows, so a function word that one text uses looks distinct by its
        counts. The grammar list (STOP plus the rest of the Snowball English stop list) holds it back."""
        text = "Only once again each kettle hums on its own, but such a sound.\n"
        self.ix.add("t0000001", "du:0-1", text)
        self.ix.commit()
        got = self.ix.tiers(text)(["Only", "once", "again", "each", "own", "but", "such", "kettle", "sound"])
        self.assertEqual({w for w, (tier, _) in got.items() if tier == 2},
                         {"Only", "once", "again", "each", "own", "but", "such"})



class HolderTest(unittest.TestCase):
    """View.context names the session of a text row through the first crumb that indexes that text."""

    def test_the_holder_of_a_text_is_found_through_an_index(self):
        with tempfile.TemporaryDirectory() as d:
            ix = Index(os.path.join(d, "index.db"))
            try:
                ix.add("c1", "aaaa1111:0-3", "one text")
                ix.add("c2", "aaaa1111:3-5", "two text")
                ix.add("c3", "bbbb2222:0-1", "three")
                ix.add("c4", "aaaa11112:0-1", "a longer doc id that starts the same")
                ix.commit()
                self.assertEqual([ix.holder(x) for x in ("aaaa1111", "bbbb2222", "cccc3333")], ["c1", "c3", None])
                plan = " ".join(str(r[-1]) for r in ix.db.execute("EXPLAIN QUERY PLAN " + HOLDER, ("a:", "a;")))
                self.assertIn("USING INDEX", plan)
                self.assertNotIn("SCAN rows", plan)
            finally:
                ix.close()

class NeighborRolesTest(unittest.TestCase):
    """A dialogue link joins a turn to a turn of another role (a question and its answer). Two turns of one role
    in a row (two user messages, or two answers) are not a pair, and smoothing over them cost 2.1 points at 512
    tokens on LongMemEval-S, where only the user turns are indexed."""

    def setUp(self):
        from breadcrumbs.ingest import put_turn
        from breadcrumbs.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "store"))
        self.index = Index(os.path.join(self.tmp.name, "index.db"))
        self.addCleanup(self.index.close)
        self.put = lambda role, text, prev=None: put_turn(self.store, self.index, role, text, "s1", ts=0, prev=prev)

    def nbr(self, c):
        return {b for (b,) in self.index.db.execute("SELECT b FROM nbr WHERE a = ?", (c,))}

    def test_a_question_and_its_answer_are_neighbors(self):
        q = self.put("user", "how long should green tea steep")
        a = self.put("assistant", "two minutes at eighty degrees", prev=q)
        self.assertEqual((self.nbr(q), self.nbr(a)), ({a}, {q}))

    def test_two_turns_of_one_role_in_a_row_are_not_neighbors(self):
        u1 = self.put("user", "how long should green tea steep")
        u2 = self.put("user", "and what about black tea", prev=u1)
        a = self.put("assistant", "four minutes for black tea", prev=u2)
        self.assertEqual((self.nbr(u1), self.nbr(u2), self.nbr(a)), (set(), {a}, {u2}))


if __name__ == "__main__":
    unittest.main()
