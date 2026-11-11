"""Git-like history over the store: commits are crumbs, refs are small files.

A commit crumb has kind "commit", kids = the root crumb ids it records,
heritage = [parent commit id], meta = {"ts": seconds, "msg": text}. Its id
covers all of these, so the commit chain is a Merkle chain: the causal spine.
Refs are the only mutable state; each ref file holds one commit id.
"""

import os
import re
import time

from .crumbs import Crumb, get_crumb, put_crumb

_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class Refs:
    def __init__(self, path, readonly=False):
        if readonly:
            if not os.path.isdir(path):
                raise FileNotFoundError(path)
        else:
            os.makedirs(path, exist_ok=True)
        self.path, self.readonly = path, readonly

    def _file(self, name):
        if not _NAME.match(name):
            raise ValueError("bad ref name: %r" % name)
        return os.path.join(self.path, name)

    def get(self, name):
        try:
            with open(self._file(name), encoding="ascii") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def set(self, name, crumb_id):
        if self.readonly:
            raise PermissionError("read-only refs: %s" % self.path)
        path = self._file(name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii") as f:
            f.write(crumb_id + "\n")
        os.replace(tmp, path)

    def names(self):
        return sorted(n for n in os.listdir(self.path) if _NAME.match(n) and not n.endswith(".tmp"))


def commit(store, refs, name, roots, msg="", ts=None):
    """Record roots on ref `name`; the old ref value becomes the parent."""
    parent = refs.get(name)
    crumb = Crumb("commit", msg[:60], kids=list(roots), heritage=[parent] if parent else [],
                  meta={"ts": int(time.time() if ts is None else ts), "msg": msg})
    cid = put_crumb(store, crumb)
    refs.set(name, cid)
    return cid


def log(store, cid):
    """Walk first parents from cid: (commit id, Crumb), newest first."""
    while cid:
        crumb = get_crumb(store, cid)
        yield cid, crumb
        cid = crumb.heritage[0] if crumb.heritage else None
