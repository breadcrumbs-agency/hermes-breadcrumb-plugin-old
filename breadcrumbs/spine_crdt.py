"""SpineCasCrdt: concurrent edits of one text merge in any order to one result.

The idea is from TrikeShed (`crdt/PijulCrdt.kt`, `cas/LineCas.kt`, `cas/LineCasSourceRanges.kt`); the unit here
is a line, the way the spine of lines.py is, and ids are CRC32C.

- The spine of a text is its lines, trimmed, with blank lines dropped. Each spine line has a CRC32C id (an
  ordinal after a dot when a different line has the same CRC32C) and an ordinal. `Line.index` maps the
  ordinal back to the raw line, so a citation or a merge locus names a place in the file.
- A patch is made against one base text. Its positions are base line numbers: Insert(pos, lines) puts the lines
  before base line `pos`; Delete(pos, n) tombstones base lines pos..pos+n-1. A patch never sees another patch,
  so the order in which patches arrive changes nothing. Its id is the CRC32C of its one canonical encoding
  (`Patch.encode`), so two branches that made the byte-identical edit hold ONE patch, and they collapse here.
- Inserts at one position order themselves by (patch encoding, change number): the same on every host. A delete
  is a tombstone: an insert before a dead line still lands there. A delete twice is a delete once.
- `render()` is a function of the set of patches. Two hosts that hold the same patches render the same bytes.

Patches share one base, which is the text every branch started from. A merged text is a new base.
"""
import difflib

from .crc32c import crc32c


def raw_lines(data):
    """The lines of data with their endings (b"\\n", b"\\r\\n" or b"\\r"), so b"".join(raw_lines(x)) == x."""
    data = bytes(data)
    return data.splitlines(keepends=True) if data else []


class Ids:
    """Line ids: 8 hex digits of the CRC32C of the trimmed line; a different line with the same CRC32C gets
    ".1", ".2", ... in the order it was first seen (as store.py does)."""

    def __init__(self):
        self.seen = {}

    def of(self, text):
        text = bytes(text)
        crc = crc32c(text)
        same = self.seen.setdefault(crc, [])
        if text not in same:
            same.append(text)
        n = same.index(text)
        return "%08x" % crc if n == 0 else "%08x.%d" % (crc, n)


class Line:
    """A spine line: `ordinal` among the surviving lines, `index` among all raw lines, `id`, and `text` (trimmed)."""
    __slots__ = ("ordinal", "index", "id", "text")

    def __init__(self, ordinal, index, id_, text):
        self.ordinal, self.index, self.id, self.text = ordinal, index, id_, text

    def __eq__(self, other):
        return isinstance(other, Line) and (self.ordinal, self.index, self.id, self.text) == (
            other.ordinal, other.index, other.id, other.text)

    def __repr__(self):
        return "Line(%d, %d, %s)" % (self.ordinal, self.index, self.id)


def spine(data, ids=None):
    """The spine of data: its non-blank lines, trimmed, with an id, an ordinal and the raw line index of each."""
    ids = ids if ids is not None else Ids()
    out = []
    for index, raw in enumerate(raw_lines(data)):
        text = raw.strip()
        if text:
            out.append(Line(len(out), index, ids.of(text), text))
    return out


class Patch:
    """Insert and delete changes against the base `base` (a CRC32C id of the base bytes).

    changes: ("ins", pos, lines) with lines a tuple of raw lines, or ("del", pos, count).
    """

    def __init__(self, base, changes):
        self.base = base
        self.changes = tuple(
            ("ins", int(c[1]), tuple(bytes(x) for x in c[2])) if c[0] == "ins" else ("del", int(c[1]), int(c[2]))
            for c in changes)
        self._encoding = None

    def encode(self):
        """The one encoding of the patch. Its CRC32C is the id; the bytes decide the order of equal ids."""
        if self._encoding is None:
            out = [b"spine-patch 1\nbase %s\n" % self.base.encode()]
            for c in self.changes:
                if c[0] == "ins":
                    out.append(b"ins %d %d\n" % (c[1], len(c[2])))
                    out += [b"%d\n%s\n" % (len(x), x) for x in c[2]]
                else:
                    out.append(b"del %d %d\n" % (c[1], c[2]))
            self._encoding = b"".join(out)
        return self._encoding

    @property
    def id(self):
        return "%08x" % crc32c(self.encode())

    def key(self):
        return crc32c(self.encode()), self.encode()

    def __eq__(self, other):
        return isinstance(other, Patch) and self.encode() == other.encode()

    def __hash__(self):
        return hash(self.encode())


def base_id(data):
    """The id that a patch carries for its base text."""
    return "%08x" % crc32c(bytes(data))


class SpineCasCrdt:
    def __init__(self, base):
        self.base_bytes = bytes(base)
        self.base = raw_lines(self.base_bytes)
        self.base_id = base_id(self.base_bytes)
        self.patches = {}  # encoding -> Patch

    def diff(self, text):
        """The patch that turns the base into text."""
        new = raw_lines(text)
        changes = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, self.base, new, autojunk=False).get_opcodes():
            if tag in ("replace", "delete"):
                changes.append(("del", i1, i2 - i1))
            if tag in ("replace", "insert"):
                changes.append(("ins", i1, tuple(new[j1:j2])))
        return Patch(self.base_id, changes)

    def apply(self, patch):
        """Add a patch. True when it was new: a patch that is already here (the same encoding) changes nothing."""
        if patch.base != self.base_id:
            raise ValueError("the patch is against base %s, not %s" % (patch.base, self.base_id))
        n = len(self.base)
        for c in patch.changes:
            if c[0] == "ins" and not 0 <= c[1] <= n:
                raise ValueError("insert at %d: the base has %d lines" % (c[1], n))
            if c[0] == "del" and not (c[2] >= 0 and 0 <= c[1] and c[1] + c[2] <= n):
                raise ValueError("delete of %d lines at %d: the base has %d lines" % (c[2], c[1], n))
        if patch.encode() in self.patches:
            return False
        self.patches[patch.encode()] = patch
        return True

    def render(self):
        dead = set()
        inserts = {}  # base position -> [(patch key, change number, lines)]
        for patch in self.patches.values():
            for number, c in enumerate(patch.changes):
                if c[0] == "del":
                    dead.update(range(c[1], c[1] + c[2]))
                else:
                    inserts.setdefault(c[1], []).append((patch.key(), number, c[2]))
        out = []
        for pos in range(len(self.base) + 1):
            for _, _, lines in sorted(inserts.get(pos, ())):
                out += lines
            if pos < len(self.base) and pos not in dead:
                out.append(self.base[pos])
        return b"".join(out)

    def overlaps(self):
        """The base places that more than one distinct patch changed: [(first line, last line, [patch ids])].
        The merge keeps every change; this says where two authors worked on the same lines (each place is a
        base line range; an insert counts at the line before which it goes)."""
        touch = {}
        for patch in self.patches.values():
            for c in patch.changes:
                span = range(c[1], c[1] + c[2]) if c[0] == "del" else (c[1],)
                for line in span:
                    touch.setdefault(line, set()).add(patch.id)
        out = []
        for line in sorted(l for l, who in touch.items() if len(who) > 1):
            who = sorted(touch[line])
            if out and out[-1][1] == line - 1 and out[-1][2] == who:
                out[-1][1] = line
            else:
                out.append([line, line, who])
        return [tuple(x) for x in out]


def merge(base, texts):
    """Merge texts (each made from base) into one. Returns (merged bytes, crdt, patches): patches[i] is the patch of
    texts[i]; two texts that made the same edit have equal patches."""
    crdt = SpineCasCrdt(base)
    patches = [crdt.diff(t) for t in texts]
    for p in patches:
        crdt.apply(p)
    return crdt.render(), crdt, patches
