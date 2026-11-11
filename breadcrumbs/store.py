"""Immutable content-addressed blob store.

Address = CRC32C of the bytes, as 8 hex digits. A different blob with the same
CRC32C gets an ordinal suffix: "10e89673.1". Blobs are never changed or deleted.

Files in the store directory:
  blobs.pack  append-only compressed blocks:
              header "<2sBBIII": b"BC", codec, 0, raw_len, comp_len, crc32c(payload)
  blobs.idx   append-only 24-byte records "<IHHQII":
              crc, ordinal, flags, block_offset, offset_in_block, length
A block is written before its index records, so the index never points at
missing bytes. Every read checks the block CRC and the blob CRC.

The open index makes no Python object per blob. Row p is item p of four arrays: crc, block,
offset, length (16 bytes; the block array widens to 8 bytes when a block starts past 4 GiB).
Rows are in the order this Store read or put them. A slot table of p + 1 finds the ordinal-0
row of a crc: open addressing on crc & mask, linear probing, at most 3/4 full (it doubles).
The rows of ordinals 1, 2, ... (CRC32C collisions, rare) are in a small dict.

Two processes can write one store (the Hermes gateway and the CLI). Each flush holds an
exclusive lock on the file "lock" and first reads the index records that other writers
appended. An id that names other bytes in another writer fails loudly. A torn record from a
crashed writer is cut off. Without fcntl (Windows) use one writer per store.
"""

import contextlib
import os
import struct
import sys
import zlib
from array import array
from collections import OrderedDict

from .crc32c import crc32c
from .runtime import no_gc

try:
    import fcntl
except ImportError:  # pragma: no cover - no flock on this platform
    fcntl = None

try:  # CPython 3.14+ has zstd in the standard library.
    from compression import zstd as _zstd  # type: ignore
except Exception:  # pragma: no cover - depends on the runtime
    _zstd = None

BLOCK_SIZE = 64 * 1024
RAW, ZLIB, ZSTD = 0, 1, 2
_HDR = struct.Struct("<2sBBIII")
_REC = struct.Struct("<IHHQII")
_SLOTS = 1024  # first size of the slot table (a power of 2)
_READ = 1024 * _REC.size  # index bytes per read: a small buffer does not stay in RSS after use
_BIG_ENDIAN = sys.byteorder == "big"  # the files are little-endian


def parse_id(blob_id):
    head, _, ordinal = blob_id.partition(".")
    if len(head) != 8:
        raise KeyError(blob_id)
    return int(head, 16), int(ordinal) if ordinal else 0


def format_id(crc, ordinal):
    return "%08x" % crc if ordinal == 0 else "%08x.%d" % (crc, ordinal)


def _compress(raw, codec):
    if codec == ZSTD and _zstd is not None:
        comp = _zstd.compress(raw)
    elif codec in (ZLIB, ZSTD):
        codec, comp = ZLIB, zlib.compress(raw, 6)
    else:
        return RAW, raw
    return (codec, comp) if len(comp) < len(raw) else (RAW, raw)


def _decompress(comp, codec):
    if codec == RAW:
        return comp
    if codec == ZLIB:
        return zlib.decompress(comp)
    if codec == ZSTD and _zstd is not None:
        return _zstd.decompress(comp)
    raise ValueError("codec %d is not available in this runtime" % codec)


class Store:
    def __init__(self, path, codec=ZLIB, cache_blocks=32, readonly=False):
        """readonly: open an existing store to read; create and change no file, refuse puts."""
        if readonly:
            if not os.path.isdir(path):
                raise FileNotFoundError(path)
        else:
            os.makedirs(path, exist_ok=True)
        self.path, self.readonly = path, readonly
        self.codec = codec
        self._crc = array("I")  # the index: one item per row (see the module doc)
        self._blk = array("I")
        self._off = array("I")
        self._len = array("I")
        self._slots = array("I", [0]) * _SLOTS  # row + 1 of the ordinal-0 blob of a crc; 0 = empty
        self._used = 0  # filled slots
        self._more = {}  # crc -> [row of ordinal 1, row of ordinal 2, ...]
        self._dead = set()  # rows of index records that add no blob (see _catch_up)
        self._buf = bytearray()
        self._pending = {}  # row -> ordinal, in put order, for blobs in the open block (offset in _buf)
        self._cache = OrderedDict()
        self._cache_blocks = cache_blocks
        self._idx_path = os.path.join(path, "blobs.idx")
        self._idx_size = 0  # bytes of blobs.idx read so far (whole records)
        self._clashes = []  # (row, block, off, length): another writer's record on one of our pending ids
        self._pack = self._idx = self._lockf = None
        if not readonly:
            self._pack = open(os.path.join(path, "blobs.pack"), "ab")
            self._idx = open(self._idx_path, "ab")
            self._lockf = open(os.path.join(path, "lock"), "ab")
        self._reader = open(os.path.join(path, "blobs.pack"), "rb")
        with no_gc():  # a big load makes many short-lived tuples: skip the cyclic GC scans
            self._catch_up()

    # -- the index --------------------------------------------------------
    def _slot(self, crc):
        """Row of the ordinal-0 blob of crc, or ~slot (below 0) of the empty slot where it goes."""
        slots, crcs = self._slots, self._crc
        mask = len(slots) - 1
        h = crc & mask
        while True:
            q = slots[h]
            if not q:
                return ~h
            if crcs[q - 1] == crc:
                return q - 1
            h = (h + 1) & mask

    def _row(self, crc, ordinal):
        """Row of the id (crc, ordinal), or -1."""
        if ordinal == 0:
            p = self._slot(crc)
            return p if p >= 0 else -1
        more = self._more.get(crc)
        if more is None or not 0 < ordinal <= len(more):
            return -1
        return more[ordinal - 1]

    def _add(self, crc, found, off, length):
        """Append the row of a new blob in the open block; found = self._slot(crc).
        Return (row, ordinal)."""
        p = len(self._crc)
        self._crc.append(crc)
        self._blk.append(0)  # set at the flush
        self._off.append(off)
        self._len.append(length)
        if found >= 0:  # a CRC32C collision: the next ordinal
            more = self._more.setdefault(crc, [])
            more.append(p)
            return p, len(more)
        self._slots[~found] = p + 1
        self._used += 1
        if self._used * 4 > len(self._slots) * 3:
            self._grow(self._used)
        return p, 0

    def _set_block(self, p, block):
        try:
            self._blk[p] = block
        except OverflowError:  # a block past 4 GiB: 8 bytes per row from now on
            self._blk = array("Q", self._blk)
            self._blk[p] = block

    def _grow(self, need):
        """Make the slot table hold need crcs at most 3/4 full; place each ordinal-0 row again."""
        size = len(self._slots)
        while need * 4 > size * 3:
            size *= 2
        if size == len(self._slots):
            return
        slots, crcs, mask = array("I", [0]) * size, self._crc, size - 1
        for q in filter(None, self._slots):
            h = crcs[q - 1] & mask
            while slots[h]:
                h = (h + 1) & mask
            slots[h] = q
        self._slots = slots

    def _catch_up(self):
        """Read the whole index records appended since the last read (by any writer).

        Each record gets a row. A record that adds no blob (a copy of a record we have, an
        ordinal past the end, another writer's record on one of our pending ids) is a dead
        row: no slot points at it, and len() and ids() skip it."""
        size = os.path.getsize(self._idx_path)
        n = (size - self._idx_size) // _REC.size  # whole records; a torn last record waits
        if n <= 0:
            return
        self._grow(self._used + n)  # one resize for a big read (a store open)
        base = len(self._crc)
        if base:
            zeros = array("I", [0]) * n
            self._crc += zeros
            self._off += zeros
            self._len += zeros
            self._blk += array(self._blk.typecode, zeros)
        else:  # a store open: exact sizes, no growth steps
            self._crc, self._blk, self._off, self._len = (array("I", [0]) * n for _ in range(4))
        end = base
        with open(self._idx_path, "rb") as f:
            f.seek(self._idx_size)
            while end < base + n:
                data = f.read(min(base + n - end, _READ // _REC.size) * _REC.size)
                m = len(data) // _REC.size
                if not m:
                    break
                self._idx_size += m * _REC.size
                self._load(end, memoryview(data)[:m * _REC.size])
                end += m
        if end < base + n:  # the file was shorter than its size said: drop the unused rows
            for a in (self._crc, self._blk, self._off, self._len):
                del a[end:]

    def _load(self, r, records):
        """Fill rows r, r + 1, ... from whole index records, then link each new blob."""
        cols = []
        for view, code in ((records.cast("I")[0::6], "I"), (records.cast("H")[2::12], "H"),
                           (records.cast("Q")[1::3], "Q"), (records.cast("I")[4::6], "I"),
                           (records.cast("I")[5::6], "I")):
            col = array(code)
            col.frombytes(view.tobytes())  # one column of the records, no Python loop
            if _BIG_ENDIAN:
                col.byteswap()
            cols.append(col)
        crcs_in, ordinals_in, blocks_in, offs_in, lens_in = cols
        end = r + len(crcs_in)
        self._crc[r:end] = crcs_in
        self._off[r:end] = offs_in
        self._len[r:end] = lens_in
        try:
            self._blk[r:end] = array(self._blk.typecode, blocks_in)
        except OverflowError:  # a block past 4 GiB: 8 bytes per row from now on
            self._blk = array("Q", self._blk)
            self._blk[r:end] = blocks_in
        slots, crcs, more = self._slots, self._crc, self._more
        mask = len(slots) - 1
        used = self._used
        for p, (crc, ordinal) in enumerate(zip(crcs_in, ordinals_in), r):
            h = crc & mask
            q = slots[h]
            while q and crcs[q - 1] != crc:
                h = (h + 1) & mask
                q = slots[h]
            if not q:
                if not ordinal:  # a new crc
                    slots[h] = p + 1
                    used += 1
                    continue
                known = 0
            else:
                known = 1 + len(more.get(crc, ()))
                if ordinal == known:  # a CRC32C collision: the next ordinal
                    more.setdefault(crc, []).append(p)
                    continue
            if ordinal < known:
                mine = q - 1 if not ordinal else more[crc][ordinal - 1]
                if mine in self._pending:
                    self._clashes.append((mine, self._blk[p], self._off[p], self._len[p]))
            self._dead.add(p)
            self._len[p] = 0
        self._used = used

    @contextlib.contextmanager
    def _locked(self):
        if fcntl is None:
            yield
            return
        fcntl.flock(self._lockf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self._lockf, fcntl.LOCK_UN)

    def _resolve_clashes(self):
        """Another writer stored a blob under the id of one of ours: the same bytes share it;
        different bytes (a CRC32C collision across writers) must never share one id."""
        for p, block, off, length in self._clashes:
            if p not in self._pending:
                continue
            if self._len[p] == length and self._block(block)[off:off + length] == self._read(p):
                self._set_block(p, block)
                self._off[p] = off
                del self._pending[p]
            else:
                raise ValueError("blob %s: another writer stored different bytes under this id"
                                 % format_id(self._crc[p], self._pending[p]))
        self._clashes.clear()

    # -- public API -------------------------------------------------------
    def put(self, data):
        if self.readonly:
            raise PermissionError("read-only store: %s" % self.path)
        data = bytes(data)
        crc = crc32c(data)
        n = len(data)
        found = self._slot(crc)
        if found >= 0:
            if self._len[found] == n and self._read(found) == data:
                return format_id(crc, 0)
            for ordinal, p in enumerate(self._more.get(crc, ()), 1):
                if self._len[p] == n and self._read(p) == data:
                    return format_id(crc, ordinal)
        if n >= BLOCK_SIZE and self._buf:
            self.flush()  # a big blob gets a block of its own; the flush may read new blobs
            return self.put(data)
        p, ordinal = self._add(crc, found, len(self._buf), n)
        self._pending[p] = ordinal
        self._buf += data
        if len(self._buf) >= BLOCK_SIZE:
            self.flush()
        return format_id(crc, ordinal)

    def find(self, data):
        """The id of a blob with these bytes, or None. It writes nothing (put() writes a new blob)."""
        data = bytes(data)
        crc, n = crc32c(data), len(data)
        for again in (False, True):
            if again:
                self._catch_up()  # another writer may have added it
            found = self._slot(crc)
            if found < 0:
                continue
            if self._len[found] == n and self._read(found) == data:
                return format_id(crc, 0)
            for ordinal, p in enumerate(self._more.get(crc, ()), 1):
                if self._len[p] == n and self._read(p) == data:
                    return format_id(crc, ordinal)
        return None

    def get(self, blob_id):
        crc, ordinal = parse_id(blob_id)
        p = self._row(crc, ordinal)
        if p < 0:
            self._catch_up()  # another writer may have added it
            p = self._row(crc, ordinal)
            if p < 0:
                raise KeyError(blob_id)
        data = self._read(p)
        if crc32c(data) != crc:
            raise ValueError("blob %s fails its CRC32C check" % blob_id)
        return data

    def __contains__(self, blob_id):
        try:
            crc, ordinal = parse_id(blob_id)
        except (KeyError, ValueError):
            return False
        return self._row(crc, ordinal) >= 0

    def __len__(self):
        return len(self._crc) - len(self._dead)

    def ids(self):
        ordinals = {p: k for rows in self._more.values() for k, p in enumerate(rows, 1)}
        for p in range(len(self._crc)):
            if p not in self._dead:
                yield format_id(self._crc[p], ordinals.get(p, 0))

    def flush(self):
        if not self._buf:
            return
        raw = bytes(self._buf)
        codec, comp = _compress(raw, self.codec)
        with self._locked():
            self._catch_up()
            self._resolve_clashes()
            block = self._pack.seek(0, os.SEEK_END)
            self._pack.write(_HDR.pack(b"BC", codec, 0, len(raw), len(comp), crc32c(comp)))
            self._pack.write(comp)
            self._pack.flush()
            recs = bytearray()
            for p, ordinal in self._pending.items():  # a shared copy left _pending in _resolve_clashes
                self._set_block(p, block)
                recs += _REC.pack(self._crc[p], ordinal, 0, block, self._off[p], self._len[p])
            end = os.path.getsize(self._idx_path)
            if end % _REC.size:  # under the lock, a partial record is a crashed writer's
                os.truncate(self._idx_path, end - end % _REC.size)
            self._idx.write(recs)
            self._idx.flush()
            self._idx_size = os.path.getsize(self._idx_path)
        self._buf.clear()
        self._pending.clear()

    def close(self):
        self.flush()
        for f in (self._pack, self._idx, self._reader, self._lockf):
            if f is not None:
                f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals --------------------------------------------------------
    def _read(self, p):
        off, length = self._off[p], self._len[p]
        if p in self._pending:
            return bytes(self._buf[off:off + length])
        return self._block(self._blk[p])[off:off + length]

    def _block(self, block):
        raw = self._cache.get(block)
        if raw is not None:
            self._cache.move_to_end(block)
            return raw
        self._reader.seek(block)
        magic, codec, _r, raw_len, comp_len, crc = _HDR.unpack(self._reader.read(_HDR.size))
        comp = self._reader.read(comp_len)
        if magic != b"BC" or len(comp) != comp_len or crc32c(comp) != crc:
            raise ValueError("corrupt block at offset %d" % block)
        raw = _decompress(comp, codec)
        if len(raw) != raw_len:
            raise ValueError("block at offset %d has the wrong length" % block)
        self._cache[block] = raw
        if len(self._cache) > self._cache_blocks:
            self._cache.popitem(last=False)
        return raw
