"""Zoom: express any breadcrumb at a level from 0 (none) to 1 (all).

render(ref, level) is the one model-facing operation. ref is a blob id
("e3069283"), a collision id ("10e89673.1") or a line range ("e3069283:5-12").

  level 0      the handle only: one line with the id, kind, title and size. A session or
               span handle ends with its cue words after " | " (see Index.signature).
  level 1      everything: a text byte for byte; a crumb with all kids expanded. A binary text
               (one with a NUL byte, as a SQLite file in a seat snap) shows as one line with its size.
  0 < level    budget = level x full tokens. A crumb opens at least one step
               (its header and the handles of its parts). Then one global
               order fills the budget: every part's first line, then every
               part's next lines, in proportion to its size. A kid with no
               line kept stays a one-line handle; each omitted run of a text
               shows as "... doc:a-b". The model expands any handle or marker
               with one more small call.

What a level shows is the longest prefix of one fixed order that fits, so a
higher level never drops what a lower level showed.

Summaries: with summaries=lookup (source id -> "sum" crumb id), a crumb that has a summary
shows it as its first kid, and the summary's lines come first in the order. So a low level
gives the header, the summary (model text, under its own "sum" id) and the handles of the
parts; higher levels add the exact source lines.
"""

import bisect
import heapq
import json
import re

from .crumbs import Crumb, is_crumb
from .ingest import memory_row
from .lines import decode_spine
from .store import format_id

HANDLE, GAP, DROP = "\u25b8", "\u22ef", "\u2026"
CUED = ("sess", "span")  # kinds whose handles end with cue words
_WORD = re.compile(rb"[A-Za-z0-9_./-]{3,}")
_TOKEN = re.compile(r"[^\W_]+")  # a word below level 1, as the index splits text
_DIGIT = re.compile(rb"\d")
_OPEN, _LINE = 0, 1


def tok(text):
    """Token estimate: 4 bytes per token (no tokenizer dependency)."""
    if isinstance(text, str):
        text = text.encode("utf-8")
    return (len(text) + 3) // 4


def _b(text):
    """Exact size in bytes; the render budget is counted in bytes, so no rounding drift."""
    return len(text.encode("utf-8")) if isinstance(text, str) else len(text)


def parse_ref(ref):
    blob_id, _, rng = ref.strip().partition(":")
    if not rng:
        return blob_id, 0, None
    a, _, b = rng.partition("-")
    return blob_id, int(a or 0), (int(b) if b else None)


def _show(line):
    text = line.decode("utf-8", "replace")
    return text if text.endswith("\n") else text + "\n"


def _exact(lines):
    """The lines of one range as text, byte for byte: the pieces of a long line (split_lines) join back
    into it. One line end follows when the range does not end with one, so the next part starts a line."""
    return _show(b"".join(lines)) if lines else ""


def rank_lines(lines, mass=None):
    """Indices of the non-blank lines, most salient first (first line first).

    mass: one weight per line (Index.salience). Without it, a line counts its distinct words."""
    nonblank = [i for i, l in enumerate(lines) if l.strip()]
    if not nonblank:
        return []
    first, last = nonblank[0], nonblank[-1]
    scored = []
    for i in nonblank:
        s = lines[i].strip()
        if i == first:
            score = float("inf")
        else:
            score = (len(set(_WORD.findall(s))) if mass is None else mass[i]) / (1 + tok(lines[i])) ** 0.5
            if _DIGIT.search(s):
                score *= 1.2
            if s[:1] in b"#-*>":
                score *= 1.3
            if i == last:
                score *= 1.5
        scored.append((-score, i))
    return [i for _, i in sorted(scored)]


def rank_words(words, classify):
    """(line, word) pairs of a text below level 1, in order: the anchor line first (the first line
    with a kept word: the head of a text names its topic, as the line zoom keeps its first line
    first); then the distinct words (tier 0), then the meaningful words (tier 1), each tier by score;
    a word's first use before any repeat; then text order. Grammar words (tier 2) are left out:
    they show only at level 1."""
    tiers = classify(list(dict.fromkeys(w.lower() for ws in words for w in ws)))
    seen, keyed, anchor = set(), [], None
    for i, ws in enumerate(words):
        for k, w in enumerate(ws):
            low = w.lower()
            tier, score = tiers.get(low, (1, 0.0))
            if tier < 2:
                anchor = i if anchor is None else anchor
                keyed.append(((low in seen, i != anchor, tier, -score, i, k), (i, k)))
                seen.add(low)
    return [j for _, j in sorted(keyed)]


class _Words:
    """A text range below level 1, word by word: the kept words of a line as written, in text order,
    with " … " where the line drops words; one gap marker for each run of lines with no kept word."""

    def __init__(self, doc, start, lines, depth, boost=0.0):
        self.doc, self.start, self.lines, self.depth, self.boost = doc, start, lines, depth, boost
        self.words = [_TOKEN.findall(l.decode("utf-8", "replace")) for l in lines]
        self.count = [0]
        for ws in self.words:
            self.count.append(self.count[-1] + (1 if ws else 0))
        n = len(lines)
        self.marker = _b("%s%s %s:%d-%d\n" % ("  " * depth, GAP, doc, start + n, start + n))
        self.kept, self.shown = {}, []  # line -> sorted kept word indices; sorted lines with a kept word

    def collapsed(self):
        return self.marker if self.count[-1] else 0

    def _nb(self, a, b):
        return self.count[b] - self.count[a] > 0

    def _line(self, i, ks):
        ws = self.words[i]
        return "".join((" " if k == p + 1 else " %s " % DROP) + ws[k] if p is not None else ws[k]
                       for p, k in zip([None] + ks[:-1], ks)) + "\n"

    def delta(self, j):
        i, k = j
        have = self.kept.get(i)
        if have:
            return _b(self._line(i, sorted(have + [k]))) - _b(self._line(i, have))
        at = bisect.bisect_left(self.shown, i)
        lo = self.shown[at - 1] + 1 if at else 0
        hi = self.shown[at] if at < len(self.shown) else len(self.lines)
        markers = self._nb(lo, i) + self._nb(i + 1, hi) - self._nb(lo, hi)
        return _b(self._line(i, [k])) + markers * self.marker

    def add(self, j):
        i, k = j
        if i in self.kept:
            bisect.insort(self.kept[i], k)
        else:
            self.kept[i] = [k]
            bisect.insort(self.shown, i)

    def draw(self, out):
        pad, i, n = "  " * self.depth, 0, len(self.lines)
        while i < n:
            if i in self.kept:
                out.append(self._line(i, self.kept[i]))
                i += 1
                continue
            j = i
            while j < n and j not in self.kept:
                j += 1
            if self._nb(i, j):
                out.append("%s%s %s:%d-%d\n" % (pad, GAP, self.doc, self.start + i, self.start + j))
            i = j


class _Range:
    """A text range in a render: which lines are kept, what they cost."""

    def __init__(self, doc, start, lines, depth, boost=0.0):
        self.doc, self.start, self.lines, self.depth, self.boost = doc, start, lines, depth, boost
        self.count = [0]
        for line in lines:
            self.count.append(self.count[-1] + (1 if line.strip() else 0))
        n = len(lines)
        self.marker = _b("%s%s %s:%d-%d\n" % ("  " * depth, GAP, doc, start + n, start + n))
        self.keep = []

    def collapsed(self):
        return self.marker if self.count[-1] else 0

    def _nb(self, a, b):
        return self.count[b] - self.count[a] > 0

    def delta(self, j):
        k = bisect.bisect_left(self.keep, j)
        lo = self.keep[k - 1] + 1 if k else 0
        hi = self.keep[k] if k < len(self.keep) else len(self.lines)
        markers = self._nb(lo, j) + self._nb(j + 1, hi) - self._nb(lo, hi)
        return _b(self.lines[j]) + markers * self.marker

    def add(self, j):
        bisect.insort(self.keep, j)

    def draw(self, out):
        pad, kept, i, n = "  " * self.depth, set(self.keep), 0, len(self.lines)
        while i < n:
            if i in kept:
                out.append(_show(self.lines[i]))
                i += 1
                continue
            j = i
            while j < n and j not in kept:
                j += 1
            if self._nb(i, j):
                out.append("%s%s %s:%d-%d\n" % (pad, GAP, self.doc, self.start + i, self.start + j))
            i = j


class _Open:
    """A crumb in a render: a handle until it opens, then header + parts."""

    def __init__(self, ref, head, handle, boost=0.0):
        self.ref, self.head, self.handle, self.parts, self.open = ref, head, handle, [], False
        self.boost = boost

    def collapsed(self):
        return _b(self.handle)

    def draw(self, out):
        if not self.open:
            out.append(self.handle)
            return
        out.append(self.head)
        for part in self.parts:
            part.draw(out)


class Zoom:
    def __init__(self, store, summaries=None, cues=None, salience=None, words=None):
        """words: text -> classify(words) -> {word: (tier, score)} (Index.tiers). With it, a text below
        level 1 shows words (distinct, then meaningful); without it, whole lines (salience orders them)."""
        self.store, self.summaries, self.cues, self.salience = store, summaries, cues, salience
        self.words = words
        self._full, self._docs, self._cues, self._mass = {}, {}, {}, {}

    def reset(self):
        """Forget cached sizes and line weights (call after a summary is added; texts never change)."""
        self._full.clear()
        self._mass.clear()

    # -- nodes --------------------------------------------------------------
    def _doc_lines(self, doc):
        lines = self._docs.get(doc)
        if lines is None:
            ids = [format_id(c, o) for c, o in decode_spine(self.store.get(doc))]
            lines = [self.store.get(i) for i in ids]
            if any(b"\x00" in line for line in lines):  # binary (a SQLite file in a snap): never in a context
                lines = [b"(binary, %d bytes: not shown)\n" % sum(len(line) for line in lines)]
            self._docs[doc] = lines
        return lines

    def _node(self, ref):
        blob_id, a, b = parse_ref(ref)
        blob = self.store.get(blob_id)
        if blob[:4] == b"SP01":
            return "doc", blob_id, a, b, None
        if is_crumb(blob):
            return "crumb", blob_id, a, b, Crumb.decode(blob)
        return "line", blob_id, a, b, blob

    def parts(self, crumb, blob_id=None):
        out = ["%s:%d-%d" % (r[0], r[1], r[2]) for r in crumb.ranges] + list(crumb.kids)
        if self.summaries and blob_id and crumb.kind != "sum":
            summary = self.summaries(blob_id)
            if summary:
                out.insert(0, summary)
        return out

    def _head(self, blob_id, crumb, depth, title, full=None):
        label = " " + json.dumps(crumb.title[:40], ensure_ascii=False) if title and crumb.title else ""
        cue = self.cue_words(blob_id) if title and crumb.kind in CUED else ""
        # an opened crumb that came from IPFS names its source CID (a handle in a list stays short)
        src = " \u21d0 " + crumb.meta["cid"] if not title and crumb.meta.get("cid") else ""
        return "%s%s %s %s%s \u00b7%dt%s%s\n" % ("  " * depth, HANDLE, blob_id, crumb.kind, label,
                                                 self.full(blob_id) if full is None else full,
                                                 " | " + cue if cue else "", src)

    def cue_words(self, blob_id):
        """The cue words of a crumb, joined by spaces ("" without a cues function)."""
        if self.cues and blob_id not in self._cues:
            self._cues[blob_id] = " ".join(self.cues(self._cue_text(blob_id)))
        return self._cues.get(blob_id, "")

    def _cue_text(self, ref):
        """source() without the calls and results of this memory's own tools: the index does not hold
        them (they copy memory), and cue words compare a text with the index's counts."""
        kind, blob_id, a, b, obj = self._node(ref)
        if kind != "crumb" or obj is None:
            return self.source(ref)
        if memory_row(self.store, obj):
            return ""
        return "".join(self._cue_text(p) for p in self.parts(obj))

    def source(self, ref):
        """The exact text under ref: every range in order, without headers or summaries."""
        kind, blob_id, a, b, obj = self._node(ref)
        if kind == "line":
            return _show(obj)
        if kind == "doc":
            return _exact(self._doc_lines(blob_id)[a:b])
        return "".join(self.source(p) for p in self.parts(obj))

    def size(self, ref):
        """Bytes of the level-1 expansion."""
        if ref not in self._full:
            kind, blob_id, a, b, obj = self._node(ref)
            if kind == "line":
                n = len(obj)
            elif kind == "doc":
                n = sum(len(l) for l in self._doc_lines(blob_id)[a:b])
            else:
                self._full[ref] = 0  # placeholder while the parts are measured
                n = sum(self.size(p) for p in self.parts(obj, blob_id))
                n += _b(self._head(blob_id, obj, 0, False, (n + 3) // 4))
            self._full[ref] = n
        return self._full[ref]

    def full(self, ref):
        """Tokens of the level-1 expansion (4 bytes per token)."""
        return (self.size(ref) + 3) // 4

    def handle(self, ref, depth=0):
        """The collapsed form: one line, enough to expand it later."""
        kind, blob_id, a, b, obj = self._node(ref)
        pad = "  " * depth
        if kind == "crumb":
            return self._head(blob_id, obj, depth, True)
        if kind == "doc":
            lines = self._doc_lines(blob_id)[a:b]
            if ":" in ref:
                return "%s%s %s:%d-%d\n" % (pad, GAP, blob_id, a, a + len(lines))
            return "%s%s %s doc %d lines \u00b7%dt\n" % (pad, HANDLE, blob_id, len(lines), self.full(ref))
        return "%s%s %s line \u00b7%dt\n" % (pad, HANDLE, blob_id, self.full(ref))

    # -- rendering ----------------------------------------------------------
    def render(self, ref, level=1.0):
        level = min(1.0, max(0.0, float(level)))
        if level <= 0.0:
            return self.handle(ref)
        kind, blob_id, a, b, obj = self._node(ref)
        if level >= 1.0:
            if kind == "doc":  # the exact text, byte for byte
                return b"".join(self._doc_lines(blob_id)[a:b]).decode("utf-8", "replace")
            if kind == "crumb" and obj is not None and obj.kind == "rcpt" and "items" in obj.meta:
                m = obj.meta
                if m.get("ctx") and m["ctx"] in self.store:  # what the model saw, word for word (meta.ctx names it)
                    return self._head(blob_id, obj, 0, False) + b"".join(self._doc_lines(m["ctx"])).decode("utf-8", "replace")
                # an older receipt: each item again at its recorded level (the words may differ from what the model saw)
                return self._head(blob_id, obj, 0, False) + "".join(
                    self.render(r, lv) for r, lv in zip(m["items"], m["lv"]))
            out = []
            self._emit_full(ref, 0, out)
            return "".join(out)
        if kind == "line":
            return self.handle(ref)
        return self._greedy(ref, level * self.size(ref))

    def _emit_full(self, ref, depth, out):
        kind, blob_id, a, b, obj = self._node(ref)
        if kind == "line":
            out.append(_show(obj))
        elif kind == "doc":
            out.append(_exact(self._doc_lines(blob_id)[a:b]))
        else:
            out.append(self._head(blob_id, obj, depth, False))
            for p in self.parts(obj, blob_id):
                self._emit_full(p, depth + 1, out)

    def _make(self, ref, depth, boost=0.0):
        kind, blob_id, a, b, obj = self._node(ref)
        if kind == "crumb":
            boost = -1.0 if obj.kind == "sum" else boost  # a summary and its lines come first
            return _Open(ref, self._head(blob_id, obj, depth, False), self._head(blob_id, obj, depth, True), boost)
        text = _Words if self.words and boost >= 0 else _Range  # a summary (boost < 0) keeps whole lines
        if kind == "doc":
            return text(blob_id, a, self._doc_lines(blob_id)[a:b], depth, boost)
        return text(blob_id, 0, [obj], depth, boost)

    def _push(self, node, path, heap, rank=None):
        if isinstance(node, _Open):
            heapq.heappush(heap, (node.boost, path, 0, _OPEN, 0))
            return
        if isinstance(node, _Words):
            order = rank_words(node.words, rank)
        else:  # line weights only come from salience; a words classifier does not weigh lines
            order = rank_lines(node.lines, rank(node.lines) if rank and not self.words else None)
        for k, j in enumerate(order):
            heapq.heappush(heap, (k / len(order) + node.boost, path, k, _LINE, j))

    def _open(self, node, path, depth, heap, nodes, rank=None):
        node.open = True
        kind, blob_id, a, b, obj = self._node(node.ref)
        for i, p in enumerate(self.parts(obj, blob_id)):
            child = self._make(p, depth + 1, node.boost)
            node.parts.append(child)
            nodes[path + (i,)] = child
            self._push(child, path + (i,), heap, rank)
        return _b(node.head) + sum(c.collapsed() for c in node.parts) - _b(node.handle)

    def _weights(self, ref):
        """The ranking aid of one render, from the exact text under ref: the word classes (words) or
        the line weights (salience), or None."""
        fn = self.words or self.salience
        if not fn:
            return None
        if ref not in self._mass:
            if len(self._mass) >= 256:  # a long-lived provider renders many crumbs: keep the cache small
                self._mass.clear()
            self._mass[ref] = fn(self.source(ref))
        return self._mass[ref]

    def _greedy(self, ref, budget):
        mass = self._weights(ref)
        root = self._make(ref, 0)
        nodes, heap = {(): root}, []
        total = root.collapsed()
        if isinstance(root, _Open):  # any level above 0 opens one step
            total += self._open(root, (), 0, heap, nodes, mass)
        else:
            self._push(root, (), heap, mass)
        while heap:
            _frac, path, _k, what, j = heapq.heappop(heap)
            node = nodes[path]
            if what == _OPEN:
                trial = _b(node.head) - _b(node.handle)
                kind, blob_id, a, b, obj = self._node(node.ref)
                trial += sum(self._make(p, len(path) + 1).collapsed() for p in self.parts(obj, blob_id))
                if total + trial > budget:
                    break
                total += self._open(node, path, len(path), heap, nodes, mass)
            else:
                cost = node.delta(j)
                if total + cost > budget:
                    break
                node.add(j)
                total += cost
        out = []
        root.draw(out)
        return "".join(out)
