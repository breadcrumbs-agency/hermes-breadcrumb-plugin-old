import itertools
import random
import unittest

from breadcrumbs import spine_crdt as sc
from breadcrumbs.spine_crdt import Patch, SpineCasCrdt, merge, spine

BASE = b"one\ntwo\nthree\nfour\nfive\n"


class SpineTest(unittest.TestCase):
    def test_blank_lines_drop_and_the_index_maps_back_to_the_raw_line(self):
        lines = spine(b"  a \n\n\r\n b\r\n\nc")
        self.assertEqual([(l.ordinal, l.index, l.text) for l in lines], [(0, 0, b"a"), (1, 3, b"b"), (2, 5, b"c")])

    def test_the_same_text_has_the_same_id_and_a_clash_gets_an_ordinal(self):
        ids = sc.Ids()
        a = ids.of(b"x")
        self.assertEqual(ids.of(b"x"), a)
        self.assertEqual(len(a), 8)
        ids.seen[0xDEADBEEF] = [b"first"]  # a different line with the same CRC32C
        orig = sc.crc32c
        sc.crc32c = lambda data, crc=0: 0xDEADBEEF
        try:
            self.assertEqual(ids.of(b"second"), "deadbeef.1")
            self.assertEqual(ids.of(b"first"), "deadbeef")
        finally:
            sc.crc32c = orig


class MergeTest(unittest.TestCase):
    def test_no_patch_renders_the_base(self):
        self.assertEqual(SpineCasCrdt(BASE).render(), BASE)

    def test_separate_edits_both_land(self):
        a = BASE.replace(b"two\n", b"TWO\n")
        b = BASE.replace(b"five\n", b"five\nsix\n")
        out, _, _ = merge(BASE, [a, b])
        self.assertEqual(out, b"one\nTWO\nthree\nfour\nfive\nsix\n")

    def test_the_order_of_arrival_changes_nothing(self):
        texts = [BASE.replace(b"two\n", b"TWO\n"), BASE.replace(b"two\n", b"2\n"), BASE.replace(b"one\n", b""),
                 BASE.replace(b"four\n", b"four\nfour and a half\n"), BASE + b"six\n", b"zero\n" + BASE]
        want = None
        for order in itertools.permutations(texts):
            out, _, _ = merge(BASE, order)
            want = want or out
            self.assertEqual(out, want)
        self.assertIn(b"TWO\n", want)
        self.assertIn(b"2\n", want)  # a replace on both sides keeps both: no side wins
        self.assertNotIn(b"\ntwo\n", b"\n" + want)

    def test_random_edits_converge(self):
        rnd = random.Random(7)
        base = b"".join(b"line %d\n" % i for i in range(30))
        for _ in range(40):
            texts = []
            for _ in range(4):
                lines = base.splitlines(keepends=True)
                for _ in range(rnd.randint(1, 4)):
                    at = rnd.randint(0, len(lines))
                    if rnd.random() < 0.5 and at < len(lines):
                        del lines[at:at + rnd.randint(1, 3)]
                    else:
                        lines[at:at] = [b"new %d\n" % rnd.randint(0, 9)] * rnd.randint(1, 2)
                texts.append(b"".join(lines))
            first, _, _ = merge(base, texts)
            for _ in range(3):
                rnd.shuffle(texts)
                self.assertEqual(merge(base, texts)[0], first)

    def test_the_same_edit_on_two_branches_is_one_patch(self):
        a = BASE.replace(b"two\n", b"TWO\n")
        out, crdt, (p, q, _) = merge(BASE, [a, a, a])
        self.assertEqual(p.id, q.id)
        self.assertEqual(len(crdt.patches), 1)
        self.assertEqual(out, a)

    def test_apply_twice_is_a_no_op(self):
        crdt = SpineCasCrdt(BASE)
        p = crdt.diff(BASE.replace(b"two\n", b"TWO\n"))
        self.assertTrue(crdt.apply(p))
        self.assertFalse(crdt.apply(Patch(p.base, p.changes)))
        self.assertEqual(crdt.render(), b"one\nTWO\nthree\nfour\nfive\n")

    def test_a_delete_of_one_line_twice_is_one_delete(self):
        out, _, _ = merge(BASE, [BASE.replace(b"two\n", b""), BASE.replace(b"two\nthree\n", b"")])
        self.assertEqual(out, b"one\nfour\nfive\n")

    def test_an_insert_before_a_dead_line_still_lands_there(self):
        out, _, _ = merge(BASE, [BASE.replace(b"three\n", b""), BASE.replace(b"three\n", b"two and a half\nthree\n")])
        self.assertEqual(out, b"one\ntwo\ntwo and a half\nfour\nfive\n")

    def test_the_bytes_survive_exactly(self):
        base = b"a\r\nb\r\n\r\nc"
        out, _, _ = merge(base, [base.replace(b"b\r\n", b"B\r\n"), base + b"\nd"])
        self.assertEqual(out, b"a\r\nB\r\n\r\nc\nd")

    def test_empty_base_and_no_trailing_newline(self):
        out, _, _ = merge(b"", [b"x\n", b"y"])
        self.assertEqual(out, b"x\ny")

    def test_overlaps_name_where_two_authors_worked_on_the_same_lines(self):
        _, crdt, (p, q, r) = merge(BASE, [BASE.replace(b"two\n", b"A\n"), BASE.replace(b"two\n", b"B\n"),
                                           BASE.replace(b"five\n", b"")])
        self.assertEqual(crdt.overlaps(), [(1, 1, sorted([p.id, q.id]))])

    def test_a_patch_for_another_base_or_out_of_range_is_refused(self):
        crdt = SpineCasCrdt(BASE)
        with self.assertRaises(ValueError):
            crdt.apply(SpineCasCrdt(b"other\n").diff(b"other\nmore\n"))
        with self.assertRaises(ValueError):
            crdt.apply(Patch(crdt.base_id, [("del", 4, 3)]))
        with self.assertRaises(ValueError):
            crdt.apply(Patch(crdt.base_id, [("ins", 6, (b"x\n",))]))

    def test_the_patch_id_is_the_crc32c_of_one_encoding(self):
        p = SpineCasCrdt(BASE).diff(BASE + b"six\n")
        self.assertEqual(p.id, "%08x" % sc.crc32c(p.encode()))
        self.assertEqual(Patch(p.base, p.changes).encode(), p.encode())


if __name__ == "__main__":
    unittest.main()
