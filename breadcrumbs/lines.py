"""Line CAS: a text is a spine of line blobs.

Each line (with its own line ending) is one blob in the Store. The spine is
the sequence of line CRC32Cs: the line-spine fingerprint. The spine itself is
a blob, so the document id is the CRC32C of the spine, a Merkle hash over its
lines. Equal lines are stored once across all documents and sessions.

Spine blob: b"SP01" | n u32 | k u32 | n x crc u32 | k x (pos u32, ordinal u16)
(little-endian). The k entries name the rare lines whose blob id has an
ordinal (a CRC32C collision). The crc array can be read in place as a
memoryview cast to "I".
"""

import struct
import sys

from .store import format_id, parse_id

_HEAD = struct.Struct("<4sII")
_ORD = struct.Struct("<IH")
_LITTLE = sys.byteorder == "little"


MAX_LINE = 256  # longer lines are split after a sentence end, a space, or a UTF-8 boundary
_ENDS = (b". ", b"! ", b"? ", b"; ")


def _soft_split(line, out):
    while len(line) > MAX_LINE:
        window = line[:MAX_LINE]
        cut = max(window.rfind(e) for e in _ENDS) + 2
        if cut < MAX_LINE // 4:
            cut = window.rfind(b" ") + 1
        if cut < MAX_LINE // 4:
            cut = MAX_LINE
            while cut > 1 and (line[cut] & 0xC0) == 0x80:  # never cut inside a character
                cut -= 1
        out.append(line[:cut])
        line = line[cut:]
    if line:
        out.append(line)


def split_lines(data):
    """Split at b"\\n" (endings kept); split lines over MAX_LINE bytes further.

    b"".join(split_lines(data)) == data always holds.
    """
    data = bytes(data)
    out, start = [], 0
    while True:
        end = data.find(b"\n", start)
        if end < 0:
            break
        _soft_split(data[start:end + 1], out)
        start = end + 1
    if start < len(data):
        _soft_split(data[start:], out)
    return out


def encode_spine(ids):
    crcs, ords = [], []
    for pos, blob_id in enumerate(ids):
        crc, ordinal = parse_id(blob_id)
        crcs.append(crc)
        if ordinal:
            ords.append((pos, ordinal))
    body = struct.pack("<%dI" % len(crcs), *crcs)
    tail = b"".join(_ORD.pack(p, o) for p, o in ords)
    return _HEAD.pack(b"SP01", len(crcs), len(ords)) + body + tail


def decode_spine(blob):
    magic, n, k = _HEAD.unpack_from(blob, 0)
    if magic != b"SP01":
        raise ValueError("not a spine blob")
    start = _HEAD.size
    if _LITTLE:
        crcs = memoryview(blob)[start:start + 4 * n].cast("I")
    else:  # pragma: no cover - big-endian hosts
        crcs = struct.unpack_from("<%dI" % n, blob, start)
    ords = {}
    off = start + 4 * n
    for i in range(k):
        pos, ordinal = _ORD.unpack_from(blob, off + i * _ORD.size)
        ords[pos] = ordinal
    return [(crcs[i], ords.get(i, 0)) for i in range(n)]


def put_text(store, data):
    """Store a text; return its document id (the spine blob id)."""
    ids = [store.put(line) for line in split_lines(data)]
    return store.put(encode_spine(ids))


def spine(store, doc_id):
    """The line-spine fingerprint: [(crc, ordinal), ...]."""
    return decode_spine(store.get(doc_id))


def line_ids(store, doc_id):
    return [format_id(c, o) for c, o in spine(store, doc_id)]


def get_lines(store, doc_id, start=0, stop=None):
    ids = line_ids(store, doc_id)[start:stop]
    return [store.get(i) for i in ids]


def get_text(store, doc_id):
    return b"".join(get_lines(store, doc_id))
