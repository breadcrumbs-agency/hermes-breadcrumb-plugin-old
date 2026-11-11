"""Crumbs: typed, content-addressed nodes that hold exact source ranges.

A crumb is a blob: b"CR01" + canonical JSON. Its id is the CRC32C of that
blob, so a crumb id covers its ranges, kids and heritage: a Merkle node.

Fields (short keys on disk):
  k  kind      typed heritage: doc sess turn mem fact sum rule prop rcpt root commit slot seat snap file
  t  title     short label
  r  ranges    [[doc_id, start, stop], ...] exact line ranges of source texts
  c  kids      child crumb ids, in order (the crumb holds them)
  h  heritage  crumb ids this crumb was derived from (provenance of derived facts)
  v  public    1 = may be shared; absent = private (the default)
  m  meta      small dict (session id, role, time, model, ...)
"""

import json
import sys
from dataclasses import dataclass, field

MAGIC = b"CR01"


@dataclass
class Crumb:
    kind: str
    title: str = ""
    ranges: list = field(default_factory=list)
    kids: list = field(default_factory=list)
    heritage: list = field(default_factory=list)
    public: bool = False
    meta: dict = field(default_factory=dict)

    def encode(self):
        d = {"k": self.kind}
        if self.title:
            d["t"] = self.title
        if self.ranges:
            d["r"] = [[r[0], int(r[1]), int(r[2])] for r in self.ranges]
        if self.kids:
            d["c"] = list(self.kids)
        if self.heritage:
            d["h"] = list(self.heritage)
        if self.public:
            d["v"] = 1
        if self.meta:
            d["m"] = self.meta
        return MAGIC + _dumps(d).encode("utf-8")

    @classmethod
    def decode(cls, blob):
        if not bytes(blob[:4]) == MAGIC:
            raise ValueError("not a crumb blob")
        d = json.loads(bytes(blob[4:]).decode("utf-8"))
        return cls(d["k"], d.get("t", ""), [list(r) for r in d.get("r", [])],
                   list(d.get("c", [])), list(d.get("h", [])), bool(d.get("v", 0)),
                   dict(d.get("m", {})))


# Canonical JSON. GraalPy 25's native encoder can pass False to default() ("Object of type bool is not
# JSON serializable": a receipt write in the GraalPy suite). Its pure-Python encoder gives the same bytes.
_ENC = json.JSONEncoder(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
_PURE = sys.implementation.name == "graalpy"


def _dumps(d, pure=_PURE):
    return "".join(_ENC.iterencode(d)) if pure else _ENC.encode(d)  # iterencode: the pure-Python path


def put_crumb(store, crumb):
    return store.put(crumb.encode())


def get_crumb(store, crumb_id):
    return Crumb.decode(store.get(crumb_id))


def is_crumb(blob):
    return bytes(blob[:4]) == MAGIC
