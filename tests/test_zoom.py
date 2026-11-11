import json
import re
import tempfile
import unittest
from collections import Counter

from breadcrumbs.crumbs import Crumb, get_crumb, put_crumb
from breadcrumbs.ingest import put_tree
from breadcrumbs.lines import put_text, split_lines
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom, rank_words, tok

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
         "omicron pi rho sigma tau upsilon phi chi psi omega").split()


def numbered_text(n=40):
    rows = []
    for i in range(n):
        words = " ".join(WORDS[(i * 7 + j) % len(WORDS)] for j in range(3 + i % 9))
        rows.append("line %02d: %s\n" % (i, words))
    return "".join(rows)


def content_lines(out):
    return [l for l in out.splitlines() if l and not l.lstrip().startswith(("\u25b8", "\u22ef", "\u00b7"))]


GAP = re.compile(r"\u22ef (\w{8}(?:\.\d+)?):(\d+)-(\d+)\b")


class ZoomDocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.text = numbered_text()
        self.doc = put_text(self.store, self.text.encode())
        self.zoom = Zoom(self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_level_one_of_a_doc_is_the_exact_text(self):
        self.assertEqual(self.zoom.render(self.doc, 1.0), self.text)

    def test_level_zero_is_the_handle_only(self):
        out = self.zoom.render(self.doc, 0.0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn(self.doc, out)
        self.assertEqual(content_lines(out), [])

    def test_levels_are_monotone(self):
        prev = set()
        for level in (0.1, 0.25, 0.5, 0.75, 1.0):
            cur = set(content_lines(self.zoom.render(self.doc, level)))
            self.assertTrue(prev <= cur, "level %s dropped lines" % level)
            self.assertGreater(len(cur), len(prev))
            prev = cur

    def test_partial_output_stays_near_the_budget(self):
        full = self.zoom.full(self.doc)
        for level in (0.1, 0.3, 0.6):
            self.assertLessEqual(tok(self.zoom.render(self.doc, level)), level * full + 40)

    def test_gaps_are_marked_and_expand_to_the_omitted_lines(self):
        out = self.zoom.render(self.doc, 0.3)
        gaps = GAP.findall(out)
        self.assertTrue(gaps)
        lines = self.text.splitlines(keepends=True)
        for doc, a, b in gaps:
            self.assertEqual(doc, self.doc)
            expanded = self.zoom.render("%s:%s-%s" % (doc, a, b), 1.0)
            self.assertEqual(expanded, "".join(lines[int(a):int(b)]))

    def test_shown_lines_are_verbatim_source_lines(self):
        source = set(self.text.splitlines())
        for line in content_lines(self.zoom.render(self.doc, 0.4)):
            self.assertIn(line, source)


class ZoomCrumbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.zoom = Zoom(self.store)
        self.turns = []
        for i, body in enumerate(("user asked to fix the parser crash in lines.py",
                                  "assistant found an off-by-one in split_lines and fixed it",
                                  "user confirmed the tests pass on graalpy and cpython")):
            text = (body + "\n" + numbered_text(6 + i)).encode()
            doc = put_text(self.store, text)
            self.turns.append((put_crumb(self.store, Crumb("turn", body[:30], ranges=[[doc, 0, 7 + i]])), body))
        self.sess = put_crumb(self.store, Crumb("sess", "parser fix", kids=[t for t, _ in self.turns]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_level_one_gives_a_long_line_back_byte_for_byte(self):
        """The store keeps a line over MAX_LINE as pieces. Level 1 and source() join them exactly: a JSON
        tool result (one long line) stays valid JSON, a long paragraph stays one line, and a piece cut
        inside a UTF-8 character decodes whole."""
        text = '{"output": "%s", "exit_code": 0}' % ("caf\u00e9 kettle \\n" * 60)
        data = text.encode("utf-8")
        cid = put_crumb(self.store, Crumb("tool", "t", ranges=[[put_text(self.store, data), 0, len(split_lines(data))]]))
        self.assertGreater(len(split_lines(data)), 3)  # stored as pieces
        self.assertEqual(self.zoom.source(cid), text + "\n")  # one line end after the range, none inside
        self.assertIn("\n" + text + "\n", self.zoom.render(cid, 1.0))
        self.assertEqual(json.loads(self.zoom.source(cid))["exit_code"], 0)

    def test_crumb_roundtrip_and_private_default(self):
        c = get_crumb(self.store, self.sess)
        self.assertEqual(c.kind, "sess")
        self.assertEqual(c.kids, [t for t, _ in self.turns])
        self.assertFalse(c.public)

    def test_crumb_id_is_a_merkle_hash_over_its_kids(self):
        again = put_crumb(self.store, Crumb("sess", "parser fix", kids=[t for t, _ in self.turns]))
        self.assertEqual(again, self.sess)
        other = put_crumb(self.store, Crumb("sess", "parser fix", kids=[t for t, _ in self.turns[:2]]))
        self.assertNotEqual(other, self.sess)

    def test_level_one_holds_every_kid_text(self):
        out = self.zoom.render(self.sess, 1.0)
        for _, body in self.turns:
            self.assertIn(body, out)

    def test_low_level_shows_kid_handles(self):
        out = self.zoom.render(self.sess, 0.05)
        for kid, _ in self.turns:
            self.assertIn(kid, out)

    def test_a_kid_handle_expands_in_one_small_call(self):
        kid, body = self.turns[1]
        self.assertIn(body, self.zoom.render(kid, 1.0))

    def test_first_line_of_a_range_comes_first(self):
        out = self.zoom.render(self.sess, 0.3)
        for _, body in self.turns:
            self.assertIn(body, out)


class ZoomCueTest(unittest.TestCase):
    """Session and span handles end with cue words, " | w1 w2 ...", from a cues function."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.seen = []
        self.zoom = Zoom(self.store, cues=self.cues)
        self.bodies = ["user asked how long to steep tea\n", "assistant said four minutes in the kettle\n"]
        self.turns = [self.turn(body) for body in self.bodies]
        self.sess = put_crumb(self.store, Crumb("sess", "tea", kids=self.turns))

    def cues(self, text):
        self.seen.append(text)
        return ["kettle", "tea"]

    def turn(self, body):
        doc = put_text(self.store, body.encode())
        return put_crumb(self.store, Crumb("turn", body[:20], ranges=[[doc, 0, 1]]))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_a_session_handle_ends_with_its_cue_words(self):
        self.assertTrue(self.zoom.handle(self.sess).endswith(" | kettle tea\n"))
        self.assertTrue(self.zoom.render(self.sess, 0.0).endswith(" | kettle tea\n"))

    def test_cue_words_come_from_the_exact_text_without_summaries(self):
        summary = put_crumb(self.store, Crumb("sum", "s", ranges=[[put_text(self.store, b"a model summary\n"), 0, 1]]))
        zoom = Zoom(self.store, summaries=lambda blob_id: summary, cues=self.cues)
        zoom.handle(self.sess)
        self.assertEqual(self.seen, ["".join(self.bodies)])

    def test_turn_handles_and_open_headers_have_no_cue_words(self):
        self.assertNotIn(" | ", self.zoom.handle(self.turns[0]))
        self.assertNotIn(" | ", self.zoom.render(self.sess, 1.0))

    def test_cue_words_are_made_once_per_crumb(self):
        self.zoom.handle(self.sess)
        self.zoom.handle(self.sess)
        self.assertEqual(len(self.seen), 1)

    def test_span_handles_in_a_low_level_render_show_cue_words(self):
        sess = put_tree(self.store, "sess", "long", [self.turn("turn %02d about tea\n" % i) for i in range(20)])
        spans = [l for l in self.zoom.render(sess, 0.05).splitlines() if " span " in l]
        self.assertEqual(len(spans), 2)
        self.assertTrue(all(l.endswith(" | kettle tea") for l in spans))

    def test_no_cue_words_no_bar(self):
        self.assertNotIn(" | ", Zoom(self.store, cues=lambda text: []).handle(self.sess))
        self.assertNotIn(" | ", Zoom(self.store).handle(self.sess))


class ZoomSalienceTest(unittest.TestCase):
    """A salience function weighs the lines; the order changes, the rules of section 3 do not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        rows = numbered_text().splitlines(keepends=True)
        rows[17] = "line 17: kettle\n"
        self.text = "".join(rows)
        self.doc = put_text(self.store, self.text.encode())
        self.seen = []
        self.zoom = Zoom(self.store, salience=self.salience)

    def salience(self, own):
        self.seen.append(own)
        return lambda lines: [9.0 if b"kettle" in l else 0.0 for l in lines]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_the_weighted_line_comes_after_the_first_line(self):
        self.assertIn("line 17: kettle", self.zoom.render(self.doc, 0.06))
        self.assertNotIn("line 17: kettle", Zoom(self.store).render(self.doc, 0.06))
        self.assertTrue(self.zoom.render(self.doc, 0.06).startswith("line 00:"))

    def test_salience_gets_the_exact_text_once_per_render(self):
        self.zoom.render(self.doc, 0.3)
        self.assertEqual(self.seen, [self.text])

    def test_levels_stay_monotone_and_verbatim(self):
        prev, source = set(), set(self.text.splitlines())
        for level in (0.06, 0.2, 0.5, 0.9):
            cur = set(content_lines(self.zoom.render(self.doc, level)))
            self.assertTrue(prev <= cur and cur <= source)
            prev = cur

    def test_gaps_still_expand_to_the_omitted_lines(self):
        lines = self.text.splitlines(keepends=True)
        gaps = GAP.findall(self.zoom.render(self.doc, 0.2))
        self.assertTrue(gaps)
        for doc, a, b in gaps:
            self.assertEqual(self.zoom.render("%s:%s-%s" % (doc, a, b), 1.0), "".join(lines[int(a):int(b)]))


class RankWordsTest(unittest.TestCase):
    """The word order below level 1: the anchor line first (the first line with a kept word, as the
    line zoom keeps its first line first); then the tier (distinct, then meaningful), then the score;
    every repeat after all first uses; grammar left out. One element for each rule."""

    TABLE = {"kettle": (0, 1.0), "tea": (0, 3.0), "weather": (1, 9.0), "the": (2, 0.0)}

    def classify(self, ws):
        return {w: self.TABLE.get(w, (1, 0.5)) for w in ws}

    def test_tier_then_score_then_repeats(self):
        words = [["dawn"], ["the", "kettle", "weather"], ["tea", "kettle", "walk"]]
        # the anchor dawn; then tea (0, 3.0), kettle (0, 1.0), weather (1, 9.0), walk (1, 0.5);
        # then the repeat of kettle
        self.assertEqual(rank_words(words, self.classify), [(0, 0), (2, 0), (1, 1), (1, 2), (2, 2), (2, 1)])

    def test_the_first_line_with_a_kept_word_comes_first(self):
        words = [["the"], ["walk", "the", "weather"], ["tea", "kettle", "walk"]]
        # line 0 keeps no word, so line 1 is the anchor: weather (1, 9.0), walk (1, 0.5);
        # then tea (0, 3.0), kettle (0, 1.0); then the repeat of walk
        self.assertEqual(rank_words(words, self.classify), [(1, 2), (1, 0), (2, 0), (2, 1), (2, 2)])


class ZoomWordTest(unittest.TestCase):
    """With a words function (Index.tiers), a text below level 1 shows words, not lines: the most
    distinct words first, then the meaningful words. Grammar words show only at level 1."""

    TEXT = ("The kettle boils the water too fast for green tea.\n"
            "My bike chain skips on the small cog when I climb.\n"
            "Steep the green tea for two minutes, not more.\n"
            "The weather was fine and the walk was long.\n"
            "A kettle with a thermostat stops at eighty degrees.\n"
            "---\n"
            "We packed lunch and left before the rain.\n")
    DISTINCT = {"kettle": 3.0, "tea": 2.5, "green": 2.0, "minutes": 1.5, "thermostat": 1.2}
    GRAMMAR = {"the", "for", "on", "my", "and", "was", "not", "too", "a", "i", "we", "when", "at", "with"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.doc = put_text(self.store, self.TEXT.encode())
        self.seen = []
        self.zoom = Zoom(self.store, words=self.words)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def words(self, own):
        self.seen.append(own)

        def classify(ws):
            return {w: (0, self.DISTINCT[w]) if w in self.DISTINCT else ((2, 0.0) if w in self.GRAMMAR else (1, 1.0))
                    for w in ws}
        return classify

    def shown(self, out):
        """The words of the content lines, in order (the ellipsis marks dropped)."""
        return [w for l in content_lines(out) for w in l.split() if w != "\u2026"]

    def test_below_one_the_words_are_source_words_in_text_order(self):
        source = re.findall(r"[^\W_]+", self.TEXT)
        for level in (0.2, 0.4, 0.7, 0.95):
            got, rest = self.shown(self.zoom.render(self.doc, level)), iter(source)
            self.assertTrue(got)
            self.assertTrue(all(w in rest for w in got), "level %s: not in text order" % level)

    ANCHOR = {"kettle", "boils", "water", "fast", "green", "tea"}  # line 1 without its grammar words

    def levels(self):
        for x in range(1, 100):
            yield {w.lower() for w in self.shown(self.zoom.render(self.doc, x / 100))}

    def test_the_anchor_line_shows_in_full_before_any_other_line(self):
        # its meaningful words (boils, water, fast) come before the distinct words of later lines
        first = next(s for s in self.levels() if s - self.ANCHOR)
        self.assertTrue(self.ANCHOR <= first, first)

    def test_distinct_words_lead_in_the_anchor_line_and_after_it(self):
        self.assertTrue(next(s for s in self.levels() if s) <= set(self.DISTINCT))
        extra = next(s - self.ANCHOR for s in self.levels() if s - self.ANCHOR)
        self.assertTrue(extra <= set(self.DISTINCT), extra)

    def test_meaningful_words_come_after_every_distinct_word(self):
        got = {w.lower() for w in self.shown(self.zoom.render(self.doc, 0.5))}
        self.assertTrue(set(self.DISTINCT) <= got)
        self.assertTrue(got - set(self.DISTINCT))

    def test_grammar_words_show_only_at_level_one(self):
        got = {w.lower() for w in self.shown(self.zoom.render(self.doc, 0.99))}
        self.assertIn("weather", got)
        self.assertFalse(got & self.GRAMMAR)
        self.assertEqual(self.zoom.render(self.doc, 1.0), self.TEXT)

    def test_a_higher_level_never_removes_a_word(self):
        prev = Counter()
        for level in (0.2, 0.3, 0.45, 0.6, 0.8, 0.99):
            cur = Counter(self.shown(self.zoom.render(self.doc, level)))
            self.assertFalse(prev - cur, "level %s removed a word" % level)
            prev = cur

    def test_a_line_with_no_kept_word_is_a_gap_that_expands_exactly(self):
        lines = self.TEXT.splitlines(keepends=True)
        gaps = GAP.findall(self.zoom.render(self.doc, 0.2))
        self.assertTrue(gaps)
        for doc, a, b in gaps:
            self.assertEqual(self.zoom.render("%s:%s-%s" % (doc, a, b), 1.0), "".join(lines[int(a):int(b)]))

    def test_dropped_words_inside_a_line_show_as_an_ellipsis(self):
        self.assertIn(" \u2026 ", self.zoom.render(self.doc, 0.3))

    def test_the_render_stays_within_the_budget(self):
        size = self.zoom.size(self.doc)
        for level in (0.2, 0.4, 0.7):
            self.assertLessEqual(len(self.zoom.render(self.doc, level).encode()), level * size)

    def test_the_words_function_gets_the_exact_text_once_per_render(self):
        self.zoom.render(self.doc, 0.4)
        self.assertEqual(self.seen, [self.TEXT])

    def test_a_crumb_opens_one_step_and_its_text_shows_words(self):
        turn = put_crumb(self.store, Crumb("turn", "kettle", ranges=[[self.doc, 0, 7]]))
        out = self.zoom.render(turn, 0.4)
        self.assertTrue(out.startswith("\u25b8 %s turn" % turn))
        self.assertIn("kettle", self.shown(out))
        self.assertFalse({w.lower() for w in self.shown(out)} & self.GRAMMAR)

    def test_a_summary_keeps_whole_lines(self):
        line = "The user wants the kettle at eighty degrees for green tea.\n"
        summary = put_crumb(self.store, Crumb("sum", "s", ranges=[[put_text(self.store, line.encode()), 0, 1]]))
        turn = put_crumb(self.store, Crumb("turn", "kettle", ranges=[[self.doc, 0, 7]]))
        zoom = Zoom(self.store, summaries=lambda blob_id: summary if blob_id == turn else None, words=self.words)
        self.assertIn(line, zoom.render(turn, 0.4))


if __name__ == "__main__":
    unittest.main()
