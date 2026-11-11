"""Search index over crumb ranges: SQLite FTS5 with BM25 (standard library).

The index is derived data. The store is the record; the index can be rebuilt
from it at any time. Each row is one range of one crumb, so every hit names
its exact source: (crumb id, "doc:a-b"). The FTS5 table is contentless: the
text lives once, in the store, not again in the index.

One table is not derived: `heat` (id, last access time, uses) records which crumbs the
model opened. A rebuild of the index must keep it. `sums` (source -> summary) is derived:
the "sum" crumbs name their source in their heritage.
"""

import math
import os
import re
import sqlite3
from collections import Counter
from urllib.parse import quote

_TERM = re.compile(r"\w+", re.UNICODE)
_CUE = re.compile(r"(?<![^\W_])[^\W\d_][^\W_]{2,}")  # a cue word: starts a token with a letter, 3+ characters
_ESC = re.compile(r"\\[nrt]")  # an escaped newline or tab in logged text
_TOK = re.compile(r"[^\W_]+")  # a token as the index counts it
_HEX = re.compile(r"(?=.*\d)[0-9a-f]{3,}")  # an id or a part of one, not a word
STOP = frozenset("""a an the and or of to in on at for with by from is are was were be been being do does did
done have has had i you he she it we they me my your his her its our their what when where which who whom
why how that this these those there here as about into over after before during than then so if not no
yes can could would should will shall may might must just also very much many any some more most other""".split())
# The zoom's grammar words (Index.tiers): STOP plus the rest of the Snowball English stop list. In a small
# store, a function word that one text uses looks distinct by its counts; this list keeps it for level 1.
GRAMMAR = STOP | frozenset("""myself ours ourselves yours yourself yourselves him himself hers herself itself them
theirs themselves am having doing ought but because until while against between through above below up down
out off under again further once all both each few nor only own same such too""".split())


# The first crumb that indexes a range of one text (a ref "doc:a-b"), through the index on rows(ref).
HOLDER = "SELECT crumb FROM rows WHERE ref >= ? AND ref < ? ORDER BY id LIMIT 1"


def fts_query(text):
    """Any user text -> a safe FTS5 query: distinct non-stopwords, quoted, OR-ed."""
    terms = list(dict.fromkeys(t.lower() for t in _TERM.findall(text)))
    terms = [t for t in terms if t not in STOP] or terms
    return " OR ".join('"%s"' % t for t in terms)


def g2(a, b, c, d):
    """Dunning's G2 for one word. a, b: its count in the text and in the rest; c, d: all other
    tokens in the text and in the rest. Positive when the text uses the word more than the rest."""
    n = a + b + c + d
    g = 0.0
    for o, row, col in ((a, a + c, a + b), (b, b + d, a + b), (c, a + c, c + d), (d, b + d, c + d)):
        if o > 0:
            g += o * math.log(o * n / (row * col))
    return 2 * g if a * (b + d) > b * (a + c) else -2 * g


class Index:
    def __init__(self, path, readonly=False):
        # One connection, used from Hermes worker threads: callers hold their own lock.
        # readonly: a viewer's connection (mode=ro, autocommit). It never writes, and it holds no
        # lock between statements, so it never blocks the provider.
        if readonly:
            self.db = sqlite3.connect("file:%s?mode=ro" % quote(os.path.abspath(path)), uri=True,
                                      check_same_thread=False, isolation_level=None)
        else:
            self.db = sqlite3.connect(path, check_same_thread=False)
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS t USING fts5(text, content='', tokenize='porter unicode61')")
            self.db.execute("CREATE TABLE IF NOT EXISTS rows (id INTEGER PRIMARY KEY, crumb TEXT, ref TEXT,"
                            " kind TEXT, public INTEGER, UNIQUE (crumb, ref))")
            self.db.execute("CREATE INDEX IF NOT EXISTS rows_ref ON rows (ref)")  # holder(): the crumb of a text
            self.db.execute("CREATE TABLE IF NOT EXISTS heat (id TEXT PRIMARY KEY, accessed INTEGER, uses INTEGER)")
            # Dialogue neighbors, stored both ways, so one primary-key lookup finds prev and next.
            self.db.execute("CREATE TABLE IF NOT EXISTS nbr (a TEXT, b TEXT, PRIMARY KEY (a, b)) WITHOUT ROWID")
            self.db.execute("CREATE TABLE IF NOT EXISTS sums (src TEXT PRIMARY KEY, sum TEXT) WITHOUT ROWID")
            # Live supersede tags (tags.py): a row for each key of the old crumb (its id, its text refs).
            self.db.execute("CREATE TABLE IF NOT EXISTS tags (key TEXT, old TEXT, new TEXT, n INTEGER,"
                            " PRIMARY KEY (key, old, new)) WITHOUT ROWID")
            self.db.execute("CREATE INDEX IF NOT EXISTS tags_old ON tags (old, new)")
        # Stems come from a private in-memory FTS5 table. Its writes never open a transaction on the
        # index, so cue words and line weights hold no lock on it (only reads touch the index).
        self.stem = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.stem.execute("CREATE VIRTUAL TABLE cw USING fts5(w, tokenize='porter unicode61')")
        self.stem.execute("CREATE VIRTUAL TABLE cwv USING fts5vocab('cw', 'instance')")
        self._tokens = None  # (rows, tokens, terms) of the whole index, see _totals()
        # Caches for the cue words and the zoom (a session's views ask for thousands of texts):
        # each word's stems (the tokenizer never changes) and each stem's (docs, count) in the index,
        # which _fresh() clears when the index changes.
        self._stem_of, self._vocab, self._seen = {}, {}, None

    def signature(self, text, k=8):
        """Up to k cue words of a text: the words it uses more than the rest of the index.

        Dunning's G2 over the index's own porter stems (topic signatures, Lin and Hovy 2000); the
        counts come from the index, so nothing is added at write time. A word shows as written at
        its first use (lowercase if the text also has it in lowercase); the words keep the order
        of first use. The text should be in the index. With no other text there are no cues."""
        form, own = {}, Counter()
        text = _ESC.sub(" ", text)
        for w in _CUE.findall(text):
            low = w.lower()
            if low not in STOP and not _HEX.fullmatch(low):
                own[low] += 1
                if low not in form or w == low:
                    form[low] = w
        if not own:
            return []
        words = list(own)
        stems = {}
        for stem, i in self._word_stems(words):
            stems.setdefault(stem, []).append(i)
        total = self._totals()[1]
        n1 = len(_TOK.findall(text))
        scored = []
        for stem, ids in stems.items():
            a = sum(own[words[i]] for i in ids)
            b = max(0, self._count(stem)[1] - a)
            s = g2(a, b, max(0, n1 - a), max(0, total - n1 - b))
            if s > 0:
                scored.append((-s, min(ids)))
        return [form[words[i]] for i in sorted(i for _, i in sorted(scored)[:k])]

    def _totals(self):
        """(rows, tokens, terms) of the whole index; read again when the index grows by more than 1%."""
        db = self.db
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab('main', 't', 'row')")
        rows = db.execute("SELECT max(id) FROM rows").fetchone()[0] or 0
        if self._tokens is None or rows > 1.01 * self._tokens[0]:
            tokens, terms = db.execute("SELECT sum(cnt), count(*) FROM temp.vocab").fetchone()
            self._tokens = (rows, tokens or 0, terms or 0)
        return self._tokens

    def _fresh(self):
        """Clear the stem counts when the index changed: a commit of another connection moves
        PRAGMA data_version, and a write of this connection moves total_changes."""
        self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.vocab USING fts5vocab('main', 't', 'row')")
        seen = (self.db.execute("PRAGMA data_version").fetchone()[0], self.db.total_changes)
        if seen != self._seen:
            self._seen, self._vocab = seen, {}

    def _count(self, stem):
        """(rows that hold the stem, its count) in the index. Call _fresh() first."""
        hit = self._vocab.get(stem)
        if hit is None:
            hit = self._vocab[stem] = self.db.execute("SELECT doc, cnt FROM temp.vocab WHERE term = ?",
                                                      (stem,)).fetchone() or (0, 0)
        return hit

    def _word_stems(self, words):
        """_stems(words) for single words, in its order (stem, then i), from a cache of each word's stems.
        Each call also clears stale counts (_fresh)."""
        self._fresh()
        new = [w for w in dict.fromkeys(words) if w not in self._stem_of]
        if new:
            if len(self._stem_of) > 200000:
                self._stem_of = {}
            got = {}
            for stem, i in self._stems(new):
                got.setdefault(i, []).append(stem)
            for i, w in enumerate(new):
                self._stem_of[w] = tuple(got.get(i, ()))
        return sorted((s, i) for i, w in enumerate(words) for s in self._stem_of[w])

    def _stems(self, texts):
        """[(stem, i), ...] for each porter stem of texts[i], as the index's tokenizer makes them."""
        s = self.stem
        s.executemany("INSERT INTO cw (rowid, w) VALUES (?, ?)", enumerate(texts))
        out = s.execute("SELECT term, doc FROM cwv").fetchall()
        s.execute("DELETE FROM cw")
        return out

    def _terms(self, rows):
        """The terms (porter stems) of each row, as the index counts them: [Counter, ...]."""
        out = [Counter() for _ in rows]
        for term, doc in self._stems(rows):
            out[doc][term] += 1
        return out

    def salience(self, text):
        """Line weights for the zoom: mass(lines) -> [weight of each line]. Each distinct stem of a line
        adds its log odds, the text against the rest of the index (add-one, naive Bayes), clipped at 0."""
        odds = self._odds(text)

        def mass(lines):
            rows = [l.decode("utf-8", "replace") if isinstance(l, bytes) else l for l in lines]
            return [sum(odds.get(t, 0.0) for t in c) for c in self._terms(rows)]
        return mass

    def _odds(self, text):
        """{stem: its log odds, this text against the rest of the index (add-one), clipped at 0}."""
        own = self._terms([text])[0]
        _, total, terms = self._totals()
        n1 = sum(own.values())
        n2, v = max(0, total - n1), terms + 1
        odds = {}
        self._fresh()
        for stem, a in own.items():
            b = max(0, self._count(stem)[1] - a)
            odds[stem] = max(0.0, math.log((a + 1) / (n1 + v)) - math.log((b + 1) / (n2 + v)))
        return odds

    def tiers(self, text, common=0.1):
        """Word tiers of a fragment for the zoom below level 1: classify(words) -> {word: (tier, score)}.
        The first rule that hits decides:
          a grammar word (GRAMMAR: stop words)                      -> 2 grammar
          a stem that this text uses more than the rest (odds > 0)  -> 0 distinct, score = the odds
          a stem in at least `common` of the index rows (and 3)     -> 2 grammar
          any other word                                            -> 1 meaningful, score = its idf
        Grammar words show only at level 1, with the verbatim text."""
        odds = self._odds(text)
        rows = max(1, self._totals()[0])

        def classify(words):
            words = list(dict.fromkeys(words))
            out = {}
            for stem, i in self._word_stems(words):
                w = words[i]
                if w in out:
                    continue
                if w.lower() in GRAMMAR:
                    out[w] = (2, 0.0)
                elif odds.get(stem, 0.0) > 0.0:
                    out[w] = (0, odds[stem])
                else:
                    df = self._count(stem)[0]
                    out[w] = (2, 0.0) if df >= 3 and df >= common * rows else (1, math.log((rows + 1) / (df + 1)))
            for w in words:  # a word with no token for the index (no letter or digit)
                out.setdefault(w, (1, 0.0))
            return out
        return classify

    def set_summary(self, src, summary):
        self.db.execute("INSERT OR REPLACE INTO sums VALUES (?, ?)", (src, summary))

    def summary_of(self, src):
        row = self.db.execute("SELECT sum FROM sums WHERE src = ?", (src,)).fetchone()
        return row[0] if row else None

    def link(self, a, b):
        """Crumb b follows crumb a in one dialogue (for neighbor smoothing)."""
        self.db.executemany("INSERT OR IGNORE INTO nbr VALUES (?, ?)", ((a, b), (b, a)))

    def touch(self, blob_id, ts):
        """Record one use of a crumb (or text) at time ts."""
        self.db.execute("INSERT INTO heat VALUES (?, ?, 1) ON CONFLICT(id) DO UPDATE"
                        " SET accessed = excluded.accessed, uses = uses + 1", (blob_id, int(ts)))

    def heat(self, ids):
        """{id: (last access time, uses)} for the ids that were used."""
        ids = list(ids)
        if not ids:
            return {}
        rows = self.db.execute("SELECT id, accessed, uses FROM heat WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
        return {i: (a, u) for i, a, u in rows}

    def add(self, crumb, ref, text, kind="", public=False):
        """Index one range of one crumb. Returns False if it is already there."""
        try:
            cur = self.db.execute("INSERT INTO rows (crumb, ref, kind, public) VALUES (?, ?, ?, ?)",
                                  (crumb, ref, kind, 1 if public else 0))
        except sqlite3.IntegrityError:
            return False
        self.db.execute("INSERT INTO t (rowid, text) VALUES (?, ?)", (cur.lastrowid, text))
        return True

    def drop(self, crumb, text_of):
        """Remove a crumb from the index: its rows and its neighbor links; its neighbors are linked to
        each other, so the chain skips it. text_of(ref) gives the text that a row indexed (a contentless
        FTS5 table removes a row only with its text). Returns the number of rows removed."""
        rows = self.db.execute("SELECT id, ref FROM rows WHERE crumb = ?", (crumb,)).fetchall()
        for rid, ref in rows:
            self.db.execute("INSERT INTO t (t, rowid, text) VALUES ('delete', ?, ?)", (rid, text_of(ref)))
            self.db.execute("DELETE FROM rows WHERE id = ?", (rid,))
        near = [b for (b,) in self.db.execute("SELECT b FROM nbr WHERE a = ?", (crumb,)).fetchall()]
        self.db.execute("DELETE FROM nbr WHERE a = ? OR b = ?", (crumb, crumb))
        for i, a in enumerate(near):
            for b in near[i + 1:]:
                self.link(a, b)
        return len(rows)

    def holder(self, doc):
        """The first crumb that indexes a range of text doc, or None."""
        hit = self.db.execute(HOLDER, (doc + ":", doc + ";")).fetchall()  # all rows read: no statement stays open
        return hit[0][0] if hit else None

    def search(self, text, k=10, public_only=False, nb=0.0):
        """[(crumb, ref, score), ...], best first. Score = -bm25 (higher is better).

        nb > 0 smooths over dialogue neighbors (see link): s'(i) = s(i) + nb * max(s(prev), s(next)).
        A reply that shares no word with the query can then rank next to the question it answers.
        """
        query = fts_query(text)
        if not query:
            return []
        pub = " AND r.public = 1" if public_only else ""
        sql = ("SELECT r.crumb, r.ref, bm25(t) AS s FROM t JOIN rows r ON r.id = t.rowid WHERE t MATCH ?"
               + pub + " ORDER BY s LIMIT ?")
        hits = [(c, r, -s) for c, r, s in self.db.execute(sql, (query, k if nb <= 0 else max(3 * k, 50)))]
        if nb <= 0:
            return hits
        ref, score = {}, {}
        for c, r, s in hits:
            if c not in score:
                ref[c], score[c] = r, s
        ids = list(score)
        marks = ",".join("?" * len(ids))
        near = {}
        for x, y in self.db.execute("SELECT a, b FROM nbr WHERE a IN (%s)" % marks, ids):
            near[y] = max(near.get(y, 0.0), score[x])
        new = [c for c in near if c not in score]
        if new:
            marks = ",".join("?" * len(new))
            for c, r in self.db.execute("SELECT r.crumb, r.ref FROM rows r WHERE r.crumb IN (%s)%s ORDER BY r.id"
                                        % (marks, pub), new):
                ref.setdefault(c, r)
        order = ids + [c for c in new if c in ref]
        final = {c: score.get(c, 0.0) + nb * near.get(c, 0.0) for c in order}
        return [(c, ref[c], final[c]) for c in sorted(order, key=lambda c: -final[c])[:k]]

    def commit(self):
        self.db.commit()

    def close(self):
        if self.db.in_transaction:
            self.db.commit()
        self.db.close()
        self.stem.close()
