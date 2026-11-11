import tempfile
import unittest

from breadcrumbs.crc32c import crc32c
from breadcrumbs.lines import MAX_LINE, get_lines, get_text, put_text, spine, split_lines
from breadcrumbs.store import Store

COLLIDE_A = b"b6fea97c59021eeb625398ee"
COLLIDE_B = b"8d348ed51c6c9452dd7260db"


class LineSpineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_roundtrip_is_byte_exact(self):
        for text in (b"", b"one line no newline", b"a\nb\n", b"a\r\nb\r\n\n\nlast"):
            doc = put_text(self.store, text)
            self.assertEqual(get_text(self.store, doc), text)

    def test_same_text_same_doc_id_in_a_fresh_store(self):
        doc = put_text(self.store, b"alpha\nbeta\n")
        with tempfile.TemporaryDirectory() as other:
            with Store(other) as s2:
                self.assertEqual(put_text(s2, b"alpha\nbeta\n"), doc)

    def test_repeated_lines_are_stored_once(self):
        put_text(self.store, b"same line\n" * 1000)
        self.assertEqual(len(self.store), 2)  # one line blob + one spine blob

    def test_docs_share_line_blobs(self):
        put_text(self.store, b"shared\nonly-a\n")
        before = len(self.store)
        put_text(self.store, b"shared\nonly-b\n")
        self.assertEqual(len(self.store), before + 2)  # new line + new spine

    def test_spine_is_the_line_crc_sequence(self):
        doc = put_text(self.store, b"x\ny\nz")
        self.assertEqual([c for c, _ in spine(self.store, doc)],
                         [crc32c(b"x\n"), crc32c(b"y\n"), crc32c(b"z")])

    def test_get_lines_returns_the_exact_range(self):
        doc = put_text(self.store, b"l0\nl1\nl2\nl3\n")
        self.assertEqual(get_lines(self.store, doc, 1, 3), [b"l1\n", b"l2\n"])
        self.assertEqual(get_lines(self.store, doc, 3), [b"l3\n"])

    def test_colliding_lines_roundtrip(self):
        # Equal CRC state plus an equal suffix keeps the collision.
        a, b = COLLIDE_A + b"\n", COLLIDE_B + b"\n"
        self.assertEqual(crc32c(a), crc32c(b))
        doc = put_text(self.store, a + b)
        self.assertEqual(get_text(self.store, doc), a + b)
        self.assertEqual([o for _, o in spine(self.store, doc)], [0, 1])

    def test_long_lines_split_after_sentence_ends(self):
        para = "".join("This is sentence number %d of one long paragraph. " % i for i in range(40))
        data = para.encode() + b"\n"
        segs = split_lines(data)
        self.assertEqual(b"".join(segs), data)
        self.assertGreater(len(segs), 1)
        self.assertTrue(all(len(s) <= MAX_LINE for s in segs))
        self.assertTrue(all(s.endswith(b". ") for s in segs[:-1]))

    def test_unbreakable_line_cuts_on_a_utf8_boundary(self):
        data = ("\u00e9" * 300).encode()  # 600 bytes, no space, no sentence end
        segs = split_lines(data)
        self.assertEqual(b"".join(segs), data)
        for s in segs:
            s.decode("utf-8")  # raises if a cut split a character
            self.assertLessEqual(len(s), MAX_LINE)


if __name__ == "__main__":
    unittest.main()
