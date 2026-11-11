import contextlib
import io
import json
import multiprocessing
import os
import struct
import tempfile
import tracemalloc
import unittest

from breadcrumbs import cli
from breadcrumbs.crc32c import crc32c
from breadcrumbs import store as store_module
from breadcrumbs.store import BLOCK_SIZE, Store

# Two different 24-byte strings with the same CRC32C (found by a birthday search).
COLLIDE_A = b"b6fea97c59021eeb625398ee"
COLLIDE_B = b"8d348ed51c6c9452dd7260db"


def _forced(prefix, crc):
    """prefix + 4 bytes whose CRC32C is crc. CRC32C is linear, so 4 free bytes reach any value."""
    table = []
    for n in range(256):
        c = n
        for _ in range(8):
            c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
        table.append(c)
    by_top = {t >> 24: i for i, t in enumerate(table)}
    c, idx = crc ^ 0xFFFFFFFF, []
    for _ in range(4):  # back from the last register: each step fixes one table index
        i = by_top[c >> 24]
        idx.insert(0, i)
        c = ((c ^ table[i]) << 8) & 0xFFFFFFFF
    c, tail = crc32c(prefix) ^ 0xFFFFFFFF, bytearray()
    for i in idx:  # forward from the prefix: each byte hits its table index
        tail.append((c ^ i) & 0xFF)
        c = table[i] ^ (c >> 8)
    return prefix + bytes(tail)


def _writer(path, tag, n, out):
    """One writer process: n blobs, one flush each (many chances to race the other writer)."""
    s = Store(path)
    ids = []
    for i in range(n):
        ids.append(s.put(("%s blob %d " % (tag, i)).encode() * 3))
        s.flush()
    s.close()
    out.put((tag, ids))


def _late_writer(path, n, out):
    """A second process: it opens the store after the first flush, then adds a collision and n blobs."""
    s = Store(path)
    ids = [s.put(COLLIDE_B)] + [s.put(b"late line %d\n" % i) for i in range(n)]
    s.close()
    out.put(ids)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_roundtrip(self):
        with Store(self.dir) as s:
            i = s.put(b"hello crumbs")
            self.assertEqual(s.get(i), b"hello crumbs")

    def test_id_is_crc32c_hex(self):
        with Store(self.dir) as s:
            self.assertEqual(s.put(b"123456789"), "e3069283")

    def test_same_bytes_same_id_stored_once(self):
        with Store(self.dir) as s:
            a = s.put(b"same")
            b = s.put(b"same")
            self.assertEqual(a, b)
            self.assertEqual(len(s), 1)

    def test_crc_collision_gets_distinct_ids(self):
        self.assertEqual(crc32c(COLLIDE_A), crc32c(COLLIDE_B))
        with Store(self.dir) as s:
            a = s.put(COLLIDE_A)
            b = s.put(COLLIDE_B)
            self.assertNotEqual(a, b)
            self.assertEqual(a, "10e89673")
            self.assertEqual(b, "10e89673.1")
            self.assertEqual(s.get(a), COLLIDE_A)
            self.assertEqual(s.get(b), COLLIDE_B)

    def test_reopen_keeps_ids_and_bytes(self):
        with Store(self.dir) as s:
            a = s.put(COLLIDE_A)
            b = s.put(COLLIDE_B)
            c = s.put(b"third")
        with Store(self.dir) as s:
            self.assertEqual(len(s), 3)
            self.assertEqual(s.get(a), COLLIDE_A)
            self.assertEqual(s.get(b), COLLIDE_B)
            self.assertEqual(s.get(c), b"third")
            self.assertEqual(s.put(COLLIDE_B), b)

    def test_corrupt_pack_is_detected(self):
        with Store(self.dir) as s:
            i = s.put(b"x" * 1000 + b"tail")
        pack = os.path.join(self.dir, "blobs.pack")
        with open(pack, "r+b") as f:
            f.seek(os.path.getsize(pack) - 3)
            f.write(b"\x00\x01\x02")
        with Store(self.dir) as s:
            with self.assertRaises(ValueError):
                s.get(i)

    def test_compresses_repetitive_data(self):
        raw = 0
        with Store(self.dir) as s:
            for n in range(2000):
                data = b"turn %d: the agent read the file and wrote a short note\n" % n
                raw += len(data)
                s.put(data)
        self.assertLess(os.path.getsize(os.path.join(self.dir, "blobs.pack")), raw // 4)

    def test_blob_larger_than_block(self):
        big = bytes(range(256)) * 2000  # 512000 bytes
        with Store(self.dir) as s:
            i = s.put(big)
        with Store(self.dir) as s:
            self.assertEqual(s.get(i), big)


def _line(i):
    return b"line %d of a short note\n" % i


class IndexTest(unittest.TestCase):
    """The open id index: each id stays findable while the index grows, flushes and reopens."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_put_id_is_found_while_the_index_grows(self):
        n = 5000  # past several growth steps of the index, with blobs in flushed and open blocks
        with Store(self.dir) as s:
            ids = [s.put(_line(i)) for i in range(n)]
            self.assertEqual(len(set(ids)), n)
            self.assertEqual(len(s), n)
            for i, blob_id in enumerate(ids):
                self.assertIn(blob_id, s)
                self.assertEqual(s.get(blob_id), _line(i))
            self.assertEqual([s.put(_line(i)) for i in range(n)], ids)
            self.assertEqual(len(s), n)
            self.assertEqual(sorted(s.ids()), sorted(ids))
        with Store(self.dir) as s:
            self.assertEqual(len(s), n)
            self.assertEqual(sorted(s.ids()), sorted(ids))
            for i, blob_id in enumerate(ids):
                self.assertEqual(s.get(blob_id), _line(i))
            self.assertEqual(s.put(_line(7)), ids[7])
            self.assertEqual(len(s), n)
            more = [s.put(b"a line put after the reopen %d\n" % i) for i in range(n)]
            self.assertEqual(len(s), 2 * n)
            for i, blob_id in enumerate(more):
                self.assertEqual(s.get(blob_id), b"a line put after the reopen %d\n" % i)
            self.assertEqual(s.get(ids[-1]), _line(n - 1))

    def test_crc_collisions_keep_their_ordinals_while_the_index_grows(self):
        crc = crc32c(COLLIDE_A)
        third = _forced(b"a third line with the same crc ", crc)
        fourth = _forced(b"a fourth line with the same crc ", crc)
        self.assertEqual((crc32c(third), crc32c(fourth)), (crc, crc))
        with Store(self.dir) as s:
            a = s.put(COLLIDE_A)
            filler = [s.put(_line(i)) for i in range(3000)]
            b = s.put(COLLIDE_B)
            s.flush()
            filler += [s.put(b"more filler %d\n" % i) for i in range(3000)]
            c = s.put(third)
            self.assertEqual((a, b, c), ("10e89673", "10e89673.1", "10e89673.2"))
            self.assertEqual([s.put(x) for x in (third, COLLIDE_B, COLLIDE_A)], [c, b, a])
            self.assertEqual([s.get(x) for x in (a, b, c)], [COLLIDE_A, COLLIDE_B, third])
            self.assertNotIn("10e89673.3", s)
        with Store(self.dir) as s:
            self.assertEqual([s.get(x) for x in (a, b, c)], [COLLIDE_A, COLLIDE_B, third])
            self.assertEqual([s.put(x) for x in (COLLIDE_B, third, COLLIDE_A)], [b, c, a])
            self.assertEqual(len(s), 6003)
            d = s.put(fourth)
            self.assertEqual(d, "10e89673.3")
            self.assertEqual(sorted(s.ids()), sorted([a, b, c, d] + filler))
        with Store(self.dir) as s:
            self.assertEqual(s.get(d), fourth)
            self.assertEqual(len(s), 6004)

    def test_membership_and_len(self):
        with Store(self.dir) as s:
            self.assertEqual(len(s), 0)
            self.assertNotIn("10e89673", s)
            a = s.put(COLLIDE_A)
            self.assertIn(a, s)  # still in the open block
            self.assertNotIn("10e89673.1", s)
            b = s.put(COLLIDE_B)
            s.flush()
            self.assertIn(a, s)
            self.assertIn(b, s)
            self.assertEqual(len(s), 2)
            for absent in ("10e89673.2", "10e89674", "00000000", "10e89673.-1"):
                self.assertNotIn(absent, s)
                with self.assertRaises(KeyError):
                    s.get(absent)
            for bad in ("", "10e8967", "10e896730", "zzzzzzzz", "10e89673.x"):
                self.assertNotIn(bad, s)

    def test_duplicate_and_gap_records_count_once(self):
        with Store(self.dir) as s:
            x = s.put(b"a line written once")
        idx = os.path.join(self.dir, "blobs.idx")
        with open(idx, "rb") as f:
            rec = f.read()
        crc, _ordinal, _flags, block, off, length = struct.unpack("<IHHQII", rec)
        with open(idx, "ab") as f:
            f.write(rec)  # the same record again
            f.write(struct.pack("<IHHQII", crc, 5, 0, block, off, length))  # an ordinal past the end
            f.write(struct.pack("<IHHQII", crc32c(b"never stored"), 1, 0, block, off, length))  # no ordinal 0
        with Store(self.dir) as s:
            y = s.put(b"a line after the odd records")
            self.assertEqual(len(s), 2)
            self.assertEqual(sorted(s.ids()), sorted([x, y]))
            self.assertNotIn(x + ".1", s)
            self.assertNotIn(x + ".5", s)
            self.assertNotIn("%08x" % crc32c(b"never stored"), s)
            self.assertNotIn("%08x.1" % crc32c(b"never stored"), s)
        with Store(self.dir) as s:
            self.assertEqual(len(s), 2)
            self.assertEqual(sorted(s.ids()), sorted([x, y]))
            self.assertEqual(s.get(x), b"a line written once")
            self.assertEqual(s.get(y), b"a line after the odd records")

    def test_a_block_past_4_gib_keeps_its_offset(self):
        with Store(self.dir) as s:
            first = s.put(b"a line in the first block")
        os.truncate(os.path.join(self.dir, "blobs.pack"), 2**32 + 5)  # a sparse hole: no real disk
        with Store(self.dir) as s:
            x = s.put(b"a line past 4 GiB")
            s.flush()
            self.assertEqual(s.get(x), b"a line past 4 GiB")
        with Store(self.dir) as s:
            self.assertEqual(s.get(x), b"a line past 4 GiB")
            self.assertEqual(s.get(first), b"a line in the first block")
            self.assertEqual(len(s), 2)

    @unittest.skipUnless(hasattr(tracemalloc, "start"), "needs tracemalloc (CPython)")
    def test_the_index_costs_under_32_bytes_per_blob(self):
        n = 20000
        with Store(self.dir) as s:
            tracemalloc.start()
            try:
                for i in range(n):
                    s.put(_line(i))
                s.flush()
                used = tracemalloc.get_traced_memory()[0]
            finally:
                tracemalloc.stop()
        self.assertLess(used / n, 32)


class ReadOnlyTest(unittest.TestCase):
    def test_a_read_only_store_of_a_missing_folder_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileNotFoundError):
                Store(os.path.join(d, "store"), readonly=True)
            self.assertEqual(os.listdir(d), [])

    def test_a_read_only_store_reads_refuses_puts_and_changes_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "store")
            with Store(path) as s:
                bid = s.put(b"one line\n")
            os.remove(os.path.join(path, "lock"))
            before = {n: os.stat(os.path.join(path, n)).st_mtime_ns for n in os.listdir(path)}
            r = Store(path, readonly=True)
            self.assertEqual(r.get(bid), b"one line\n")
            with self.assertRaises(PermissionError):
                r.put(b"two\n")
            r.close()
            self.assertEqual({n: os.stat(os.path.join(path, n)).st_mtime_ns for n in os.listdir(path)}, before)


@unittest.skipIf(store_module.fcntl is None, "no flock on this platform: one writer per store")
class TwoWritersTest(unittest.TestCase):
    """Two processes may write one store (the Hermes gateway and the CLI)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_reader_sees_what_another_writer_flushed(self):
        a, b = Store(self.dir), Store(self.dir)
        x = a.put(b"written by A")
        a.flush()
        self.assertEqual(b.get(x), b"written by A")
        a.close()
        b.close()

    def test_the_same_bytes_from_two_writers_keep_one_id(self):
        a, b = Store(self.dir), Store(self.dir)
        x, y = a.put(b"same bytes"), b.put(b"same bytes")
        a.flush()
        b.flush()
        a.close()
        b.close()
        with Store(self.dir) as c:
            self.assertEqual(x, y)
            self.assertEqual(c.get(x), b"same bytes")
            self.assertEqual(len(c), 1)

    def test_a_writer_that_uses_another_writers_copy_counts_it_once(self):
        a, b = Store(self.dir), Store(self.dir)
        x, y = a.put(b"same bytes"), b.put(b"same bytes")
        a.flush()
        b.flush()  # b reads a's record and uses a's copy
        self.assertEqual(len(b), 1)
        self.assertEqual(list(b.ids()), [x])
        self.assertEqual(b.get(y), b"same bytes")
        self.assertEqual(os.path.getsize(os.path.join(self.dir, "blobs.idx")), 24)  # one record
        a.close()
        b.close()

    def test_a_collision_across_writers_fails_loudly(self):
        a, b = Store(self.dir), Store(self.dir)
        x, y = a.put(COLLIDE_A), b.put(COLLIDE_B)
        self.assertEqual(x, y)  # each writer gave ordinal 0 on its own
        a.flush()
        with self.assertRaises(ValueError):
            b.flush()  # never let one id name two different texts
        a.close()
        with Store(self.dir) as c:
            self.assertEqual(c.get(x), COLLIDE_A)

    def test_a_torn_record_from_a_crash_is_cut_off(self):
        with Store(self.dir) as a:
            x = a.put(b"before the crash")
        with open(os.path.join(self.dir, "blobs.idx"), "ab") as f:
            f.write(b"\x01" * 10)  # a writer died in the middle of a record
        with Store(self.dir) as b:
            y = b.put(b"after the crash")
        with Store(self.dir) as c:
            self.assertEqual(c.get(x), b"before the crash")
            self.assertEqual(c.get(y), b"after the crash")
            self.assertEqual(len(c), 2)

    def test_parallel_flushes_keep_every_offset_right(self):
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        procs = [ctx.Process(target=_writer, args=(self.dir, tag, 300, out)) for tag in ("p", "q")]
        for p in procs:
            p.start()
        got = dict(out.get(timeout=120) for _ in procs)
        for p in procs:
            p.join(timeout=60)
        with Store(self.dir) as c:
            for tag, ids in got.items():
                for i, blob_id in enumerate(ids):
                    self.assertEqual(c.get(blob_id), ("%s blob %d " % (tag, i)).encode() * 3)

    def test_a_reader_learns_many_blobs_of_a_second_process(self):
        a = Store(self.dir)
        x = a.put(COLLIDE_A)
        a.flush()
        ctx = multiprocessing.get_context("spawn")
        out = ctx.Queue()
        p = ctx.Process(target=_late_writer, args=(self.dir, 3000, out))
        p.start()
        ids = out.get(timeout=120)
        p.join(timeout=60)
        self.assertEqual(ids[0], x + ".1")  # the second process saw COLLIDE_A at ordinal 0
        self.assertNotIn(ids[1], a)  # not read yet: membership does not read the index file
        for i, blob_id in enumerate(ids[1:]):
            self.assertEqual(a.get(blob_id), b"late line %d\n" % i)
        self.assertEqual(a.get(ids[0]), COLLIDE_B)
        self.assertEqual(len(a), 3002)
        self.assertEqual(a.put(COLLIDE_B), ids[0])
        self.assertEqual(sorted(a.ids()), sorted([x] + ids))
        a.close()

    def test_a_big_blob_that_another_writer_flushed_keeps_one_id(self):
        big = bytes(range(256)) * 300
        self.assertGreater(len(big), BLOCK_SIZE)
        a, b = Store(self.dir), Store(self.dir)
        a.put(b"a small line in the open block")
        x = b.put(big)
        b.flush()
        self.assertEqual(a.put(big), x)  # the flush before a big blob reads b's record
        self.assertEqual(len(a), 2)
        self.assertEqual(len(list(a.ids())), 2)
        a.close()
        b.close()
        with Store(self.dir) as c:
            self.assertEqual(c.get(x), big)
            self.assertEqual(len(c), 2)

    def test_a_big_collision_after_another_writers_flush_gets_the_next_ordinal(self):
        y_big = bytes(range(256)) * 300
        x_big = _forced(bytes(reversed(y_big)), crc32c(y_big))
        self.assertEqual(crc32c(x_big), crc32c(y_big))
        a, b = Store(self.dir), Store(self.dir)
        a.put(b"a small line in the open block")
        y = b.put(y_big)
        b.flush()
        x = a.put(x_big)
        self.assertEqual(x, y + ".1")  # never the id of b's different bytes
        a.close()
        b.close()
        with Store(self.dir) as c:
            self.assertEqual(c.get(y), y_big)
            self.assertEqual(c.get(x), x_big)
            self.assertEqual(len(c), 3)


class StatsTest(unittest.TestCase):
    """The CLI counts each blob once, also when the index file holds a copy of a record."""

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(list(argv)), 0)
        return json.loads(out.getvalue())

    def test_stats_and_verify_count_a_copied_record_once(self):
        with tempfile.TemporaryDirectory() as home:
            with Store(os.path.join(home, "store")) as s:
                s.put(b"a line written once")
            idx = os.path.join(home, "store", "blobs.idx")
            with open(idx, "rb") as f:
                rec = f.read()
            with open(idx, "ab") as f:
                f.write(rec)
            stats = self.run_cli("--home", home, "stats")
            self.assertEqual((stats["blobs"], stats["unique_raw_bytes"]), (1, len(b"a line written once")))
            self.assertEqual(self.run_cli("--home", home, "verify")["blobs"], 1)

    def test_the_cli_finds_a_stored_turn_under_its_session_handle(self):
        from breadcrumbs.hermes import BreadcrumbsProvider
        with tempfile.TemporaryDirectory() as hermes_home:
            p = BreadcrumbsProvider()
            p.initialize("s1", hermes_home=hermes_home, platform="cli", agent_context="primary")
            p.sync_turn("the walrus kettle whistles", "descale the walrus kettle", session_id="s1")
            sess = p.session_crumb("s1")
            p.shutdown()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["--home", os.path.join(hermes_home, "breadcrumbs"), "find", "walrus"]), 0)
        self.assertIn(sess, out.getvalue())
        self.assertIn("walrus kettle", out.getvalue())


if __name__ == "__main__":
    unittest.main()
