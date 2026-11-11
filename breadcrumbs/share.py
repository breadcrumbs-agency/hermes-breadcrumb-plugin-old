"""Share: export public crumbs as an IPFS CAR file, import one back. Private crumbs never leave.

Locally a crumb is addressed by CRC32C. On export each part gets an IPFS CIDv1 (sha2-256):
  - each source line is a raw block (codec 0x55);
  - each public crumb is a dag-json block (codec 0x0129, keys in byte order, no spaces):
    {"kind", "title", "ranges": [{"at": a, "lines": [link, ...]}], "kids": [link], "from": [link],
     "meta": {"role", "ts"}}  where link = {"/": "<cid>"}.
Rules that keep private memory unlisted and unshared:
  - a crumb that is not public is not exported, and no export names it (no CID, no CRC id);
  - a public crumb exports only the lines of its own ranges, never the rest of the text;
  - meta keeps only "role" and "ts" (session ids and the rest stay home);
  - publish() makes a public COPY; the private original does not change.
The file is a CARv1 (header: dag-cbor {"roots", "version": 1}). No daemon and no network are
needed: `ipfs dag import FILE.car` adds it to a kubo repo when the user wants that.
"""

import base64
import hashlib
import json
import os

from .crumbs import Crumb, get_crumb, put_crumb
from .lines import decode_spine, put_text, split_lines
from .store import format_id

RAW, DAG_JSON = 0x55, 0x0129
SHA2_256 = 0x12
META_KEYS = ("role", "ts")


# -- CIDs ------------------------------------------------------------------
def varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf, pos):
    n = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7


def cid_bytes(codec, data):
    return varint(1) + varint(codec) + bytes([SHA2_256, 32]) + hashlib.sha256(data).digest()


def cid_str(cb):
    return "b" + base64.b32encode(cb).decode("ascii").lower().rstrip("=")


def _parse_cid(buf, pos):
    start = pos
    version, pos = read_varint(buf, pos)
    if version != 1:
        raise ValueError("only CIDv1 is supported")
    codec, pos = read_varint(buf, pos)
    code, pos = read_varint(buf, pos)
    size, pos = read_varint(buf, pos)
    return bytes(buf[start:pos + size]), codec, code, bytes(buf[pos:pos + size]), pos + size


def dag_json(node):
    return json.dumps(node, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# -- CAR (v1) --------------------------------------------------------------
def _cbor_head(major, n):
    if n < 24:
        return bytes([major << 5 | n])
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if n < 1 << (8 * size):
            return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    raise ValueError("CBOR length too large")


def _car_header(roots):
    out = _cbor_head(5, 2) + _cbor_head(3, 5) + b"roots" + _cbor_head(4, len(roots))
    for cb in roots:
        out += b"\xd8\x2a" + _cbor_head(2, len(cb) + 1) + b"\x00" + cb  # tag 42: a CID
    return out + _cbor_head(3, 7) + b"version" + _cbor_head(0, 1)


def _cbor(buf, pos):
    """Enough CBOR to read a CAR header: uint, bytes, text, array, map, tag 42."""
    b = buf[pos]
    major, info, pos = b >> 5, b & 31, pos + 1
    if info < 24:
        n = info
    elif info <= 27:
        size = 1 << (info - 24)
        n, pos = int.from_bytes(buf[pos:pos + size], "big"), pos + size
    else:
        raise ValueError("unsupported CBOR item")
    if major == 0:
        return n, pos
    if major in (2, 3):
        raw = bytes(buf[pos:pos + n])
        return (raw if major == 2 else raw.decode("utf-8")), pos + n
    if major == 4:
        items = []
        for _ in range(n):
            item, pos = _cbor(buf, pos)
            items.append(item)
        return items, pos
    if major == 5:
        d = {}
        for _ in range(n):
            k, pos = _cbor(buf, pos)
            d[k], pos = _cbor(buf, pos)
        return d, pos
    if major == 6 and n == 42:
        v, pos = _cbor(buf, pos)
        return v[1:], pos  # drop the 0x00 multibase prefix
    raise ValueError("unsupported CBOR major type %d" % major)


def read_car(path):
    """(roots, {cid: bytes}); every block is checked against its CID."""
    with open(path, "rb") as f:
        data = f.read()
    size, pos = read_varint(data, 0)
    header, _ = _cbor(data, pos)
    pos += size
    if not isinstance(header, dict) or header.get("version") != 1:
        raise ValueError("not a CARv1 file")
    roots = [cid_str(cb) for cb in header.get("roots", [])]
    blocks = {}
    while pos < len(data):
        size, pos = read_varint(data, pos)
        end = pos + size
        cb, _codec, code, digest, start = _parse_cid(data, pos)
        block = data[start:end]
        if code != SHA2_256 or hashlib.sha256(block).digest() != digest:
            raise ValueError("block %s fails its hash" % cid_str(cb))
        blocks[cid_str(cb)] = block
        pos = end
    return roots, blocks


# -- export / import ---------------------------------------------------------
def _lines(store, doc):
    return [store.get(format_id(c, o)) for c, o in decode_spine(store.get(doc))]


class _Exporter:
    def __init__(self, store):
        self.store, self.blocks, self.done, self.withheld = store, {}, {}, 0

    def block(self, codec, data):
        cb = cid_bytes(codec, data)
        self.blocks.setdefault(cb, data)
        return cid_str(cb)

    def crumb(self, crumb_id):
        if crumb_id in self.done:
            return self.done[crumb_id]
        self.done[crumb_id] = None  # also guards cycles
        c = get_crumb(self.store, crumb_id)
        if not c.public:
            self.withheld += 1
            return None
        node = {"kind": c.kind}
        if c.title:
            node["title"] = c.title
        ranges = []
        at = c.meta.get("at") or []  # an imported crumb keeps the line offsets of its source
        for n, (doc, a, b) in enumerate(c.ranges):
            lines = _lines(self.store, doc)[a:b]
            ranges.append({"at": at[n] if n < len(at) else a,
                           "lines": [{"/": self.block(RAW, line)} for line in lines]})
        if ranges:
            node["ranges"] = ranges
        for key, ids in (("kids", c.kids), ("from", c.heritage)):
            links = [x for x in (self.crumb(i) for i in ids) if x]
            if links:
                node[key] = [{"/": x} for x in links]
        meta = {k: c.meta[k] for k in META_KEYS if k in c.meta}
        if meta:
            node["meta"] = meta
        self.done[crumb_id] = self.block(DAG_JSON, dag_json(node))
        return self.done[crumb_id]


def export_car(store, crumb_ids, path):
    """Write the public crumbs reachable from crumb_ids to a CAR file. Returns (roots, stats)."""
    ex = _Exporter(store)
    roots = [r for r in (ex.crumb(i) for i in crumb_ids) if r]
    if not roots:
        raise ValueError("nothing to share: every root crumb is private")
    cb_of = {cid_str(cb): cb for cb in ex.blocks}
    out = bytearray()
    header = _car_header([cb_of[r] for r in roots])
    out += varint(len(header)) + header
    for cb, data in ex.blocks.items():
        out += varint(len(cb) + len(data)) + cb + data
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, path)
    return roots, {"blocks": len(ex.blocks), "bytes": len(out), "withheld": ex.withheld}


def import_car(store, index, path, public=True):
    """Rebuild the CAR's crumbs as local crumbs (meta "cid" keeps the source). Returns root ids.

    public=True: public crumbs, so an export names them and a hive peer answers from them.
    public=False: private crumbs (a child's work in its parent's store). They are found by a local
    search, but no export names them and no hive peer sees them. The flag is part of a crumb's blob,
    so a CAR gives other crumb ids private than public. Each store that imports it the same way
    gets the same ids."""
    roots, blocks = read_car(path)
    done = {}

    def node(cid):
        if cid in done:
            return done[cid]
        n = json.loads(blocks[cid])
        kids = [node(k["/"]) for k in n.get("kids", [])]
        heritage = [node(h["/"]) for h in n.get("from", [])]
        ranges, texts = [], []
        for r in n.get("ranges", []):
            text = b"".join(blocks[line["/"]] for line in r["lines"])
            doc = put_text(store, text)
            ranges.append([doc, 0, len(split_lines(text))])
            texts.append(text)
        meta = {k: v for k, v in (n.get("meta") or {}).items() if k in META_KEYS}
        meta["cid"] = cid
        if any(r["at"] for r in n.get("ranges", [])):  # so that an export rebuilds the same block
            meta["at"] = [r["at"] for r in n["ranges"]]
        before = len(store)  # put returns the old id for bytes it holds: then the crumb is not new
        local = put_crumb(store, Crumb(n["kind"], n.get("title", ""), ranges, kids, heritage, bool(public), meta))
        if index is not None and len(store) > before:  # index a crumb once, and never one this store made
            for (doc, a, b), text in zip(ranges, texts):
                index.add(local, "%s:%d-%d" % (doc, a, b), text.decode("utf-8", "replace"), kind=n["kind"],
                          public=public)
        done[cid] = local
        return local

    local_roots = [node(r) for r in roots]
    store.flush()
    if index is not None:
        index.commit()
    return local_roots


def publish(store, crumb_id, deep=False):
    """A public copy of a crumb (with deep=True, of its kids too). The original stays private."""
    c = get_crumb(store, crumb_id)
    if deep:
        c.kids = [publish(store, k, True) for k in c.kids]
    c.public = True
    return put_crumb(store, c)


# -- shared projects -----------------------------------------------------------
# A project is a public "proj" crumb under the ref proj-<name>: kids = the shared crumbs, heritage =
# the heads before it. push keeps the copy that it rebuilds from its own CAR, so every peer that
# accepts the same CID holds the same crumb ids. pull joins: a fast-forward when one head descends
# from the other, else one new head that both sides compute alike (the lower id's kids first).
PROJ = "proj-"


def _ancestors(store, head):
    seen, stack = set(), [head]
    while stack:
        h = stack.pop()
        if h not in seen:
            seen.add(h)
            stack.extend(get_crumb(store, h).heritage)
    return seen


def project_add(store, refs, name, crumb_ids):
    """Add public copies of crumbs (with their kids) to a project: a new head over the old one."""
    old = refs.get(PROJ + name)
    kids = list(get_crumb(store, old).kids) if old else []
    for i in crumb_ids:
        pub = publish(store, i, deep=True)
        if pub not in kids:
            kids.append(pub)
    head = put_crumb(store, Crumb("proj", name, kids=kids, heritage=[old] if old else [], public=True))
    refs.set(PROJ + name, head)
    return head


def project_join(store, refs, name, other):
    """Join another head into the project; returns the new local head."""
    ours = refs.get(PROJ + name)
    if ours is None or ours in _ancestors(store, other):
        head = other
    elif other in _ancestors(store, ours):
        head = ours
    else:
        first, second = sorted((ours, other))
        kids = list(get_crumb(store, first).kids)
        kids += [k for k in get_crumb(store, second).kids if k not in kids]
        head = put_crumb(store, Crumb("proj", name, kids=kids, heritage=[first, second], public=True))
    refs.set(PROJ + name, head)
    return head


def push(store, refs, name, path):
    """Write the project head to a CAR file and keep the copy rebuilt from it. Returns (cid, head)."""
    head = refs.get(PROJ + name)
    if head is None:
        raise KeyError(PROJ + name)
    roots, _ = export_car(store, [head], path)
    canon = import_car(store, None, path)[0]  # our own texts are indexed already
    refs.set(PROJ + name, canon)
    return roots[0], canon


def pull(store, index, refs, name, path):
    """Import a project head from a CAR file (every block checked) and join it. Returns the head."""
    return project_join(store, refs, name, import_car(store, index, path)[0])
