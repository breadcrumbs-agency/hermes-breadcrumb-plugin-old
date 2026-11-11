"""Hive: search the breadcrumbs of other hosts within a deadline, or pass.

Each host serves its stores read-only on a loopback port (`breadcrumbs hive serve`). kubo carries that
port over the private swarm: the host runs `ipfs p2p listen PROTO /ip4/127.0.0.1/tcp/PORT`, and each
caller runs `ipfs p2p forward PROTO /ip4/127.0.0.1/tcp/LOCAL /p2p/PEER` (`hive up` runs both). The
swarm key admits the members, and libp2p checks the peer id of each end. A request and its reply are
one JSON line each, so one connection carries many requests.

The budget: a request carries "ms", the time its caller has left. The host counts it from the arrival,
as gRPC counts a timeout; the caller sends its budget less the lag it measured on that peer, and it
keeps its own deadline. A host does no work on a request after its deadline. It passes:
  busy   the host is at capacity: its queue is full, or the wait in it ("wait", ms) would make the
         request late. The caller then sends that host nothing until the wait is over (Retry-After).
  aged   the request waited in the queue until its budget was gone, or, while others wait, until less
         than one find's time (the measured cost) was left
  late   the search used up the budget (a SQLite progress handler stops the query)
A caller with no reply at its deadline marks the peer "timeout" and goes on; "down" means no
connection. Each hit names its source (crumb id, store, peer). An open sends the exact blobs; the
caller checks the CRC32C of each one and renders the text itself.
"""

import base64
import collections
import json
import queue
import re
import selectors
import socket
import sqlite3
import threading
import time

from .crc32c import crc32c
from .crumbs import Crumb, is_crumb
from .hermes import NEIGHBOR  # at import: a host whose code does not compile fails at start, not per request
from .lines import decode_spine, get_lines
from .store import format_id, parse_id
from .zoom import Zoom, parse_ref

PROTO = "/x/breadcrumbs-hive/1"
A2A = "/x/hermes-a2a/1"  # `hive up` also carries a Hermes A2A gateway port (loopback) between the hosts
PORT = 4700
MS = 150  # the budget of a remote search (milliseconds)
MAX_MS = 2000  # a host caps every budget at this
OPEN_BYTES = 1 << 20  # an open sends at most this many bytes of blobs
TICKS = 400  # SQLite VM steps between two deadline checks
WITHHELD = "\u22ef withheld\n".encode("utf-8")  # a part that a host did not send (private, or too big)


class Late(Exception):
    """The budget ran out during a search."""


def words(text):
    return set(re.findall(r"\w+", text.lower()))


def search(index, q, k, shared, deadline, clock=time.monotonic, nb=0.3):
    """index.search under a deadline: a progress handler stops the query when the deadline passes."""
    index.db.set_progress_handler(lambda: clock() > deadline, TICKS)
    try:
        return index.search(q, k, public_only=shared, nb=nb)
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e):
            raise Late() from None
        raise
    finally:
        index.db.set_progress_handler(None, 0)


def collect(store, ref, shared, room=OPEN_BYTES):
    """The blobs that render ref, {id: bytes}, and the number of parts not sent. With shared, ref must
    be a public crumb, and a private crumb under it is not sent. A part that is over the room is not sent."""
    blob_id, a, b = parse_ref(ref)
    top = store.get(blob_id)
    if shared and not (is_crumb(top) and Crumb.decode(top).public):
        raise KeyError("%s is not shared" % blob_id)
    out, withheld = {}, 0

    def add(i, data):
        nonlocal room, withheld
        if len(data) > room:
            withheld += 1
            return False
        out[i] = data
        room -= len(data)
        return True

    def walk(i, a=0, b=None):
        nonlocal withheld
        if i in out:
            return
        data = store.get(i)
        if is_crumb(data):
            c = Crumb.decode(data)
            if shared and not c.public:
                withheld += 1
            elif add(i, data):
                for doc, x, y in c.ranges:
                    walk(doc, x, y)
                for kid in c.kids:
                    walk(kid)
        elif data[:4] == b"SP01":
            if add(i, data):
                for crc, ordinal in decode_spine(data)[a:b]:
                    line = format_id(crc, ordinal)
                    if line not in out and not add(line, store.get(line)):
                        break
        else:
            add(i, data)

    walk(blob_id, a, b)
    return out, withheld


def verify(blobs):
    """Decode the blobs of an open; each one must match the CRC32C in its id."""
    out = {}
    for i, text in blobs.items():
        data = base64.b64decode(text)
        if crc32c(data) != parse_id(i)[0]:
            raise ValueError("blob %s fails its CRC32C check" % i)
        out[i] = data
    return out


class Received:
    """The blobs of one open as a store: a part that the host did not send reads as WITHHELD."""

    def __init__(self, blobs):
        self.blobs = blobs

    def get(self, blob_id):
        return self.blobs.get(blob_id, WITHHELD)


def _send(conn, lock, msg):
    try:
        with lock:
            conn.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
    except OSError:
        pass


def _shut(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class Server:
    """Serve stores read-only. stores = {name: (root, scope)}: scope "shared" serves the public rows of
    the index, "all" every row. workers = searches at once; depth = requests that may wait.
    cost = the measured time of one find (EWMA, s). When every worker is busy and the queue ahead of a
    new request, at that cost, ends past its budget, the request passes busy at once: a worker never
    spends time on an answer that would come late. An idle worker always takes a request."""

    def __init__(self, stores, host="127.0.0.1", port=PORT, workers=2, depth=32, max_ms=MAX_MS,
                 clock=time.monotonic, name=""):
        self.stores, self.max_ms, self.clock = dict(stores), float(max_ms), clock
        self.name = name or socket.gethostname()
        self.workers, self.q = workers, queue.Queue(depth)
        self.cost, self.running = 0.0, [False] * workers
        self.counts = collections.Counter()
        self.sock = socket.create_server((host, port))
        self.sock.settimeout(0.2)  # accept wakes up to see close()
        self.port = self.sock.getsockname()[1]
        self.conns, self.threads, self.closed = set(), [], False

    def start(self):
        for target, args in [(self._accept, ())] + [(self._work, (i,)) for i in range(self.workers)]:
            t = threading.Thread(target=target, args=args, name="hive", daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def close(self):
        self.closed = True
        for c in list(self.conns):
            _shut(c)
        for t in self.threads:
            t.join(5)
        self.sock.close()

    def _accept(self):
        while not self.closed:
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.conns.add(conn)
            threading.Thread(target=self._read, args=(conn,), name="hive-conn", daemon=True).start()

    def _read(self, conn):
        lock = threading.Lock()
        try:
            for line in conn.makefile("rb"):
                t_in = self.clock()
                try:
                    req = json.loads(line)
                except ValueError:
                    req = None
                if not isinstance(req, dict):
                    _send(conn, lock, {"error": "bad request"})
                    continue
                wait = self._wait(req)
                if wait is None:
                    try:
                        self.q.put_nowait((t_in, req, conn, lock))
                        continue
                    except queue.Full:
                        pass
                self.counts["busy"] += 1
                _send(conn, lock, dict({"id": req.get("id"), "pass": "busy"}, **({"wait": wait} if wait else {})))
        except OSError:
            pass
        finally:
            self.conns.discard(conn)
            _shut(conn)

    def _wait(self, req):
        """The wait (ms) that would make req late, or None: it may queue."""
        if not (self.cost and all(self.running)):
            return None
        try:
            budget = min(float(req.get("ms", MS)), self.max_ms)
        except (TypeError, ValueError):
            budget = MS
        wait = (self.q.qsize() / self.workers + 1) * self.cost * 1000.0
        return round(wait, 2) if wait > budget else None

    def _work(self, slot):
        views = {}
        try:
            for name in sorted(self.stores):  # open the stores now, so the first find does not pay the open
                try:
                    self._view(views, name)
                except Exception:  # a store that does not open answers error on each request
                    pass
            while not self.closed:
                try:
                    t_in, req, conn, lock = self.q.get(timeout=0.2)
                except queue.Empty:
                    continue
                self.running[slot], t0 = True, self.clock()
                try:
                    out = self.answer(req, t_in, views)
                finally:
                    self.running[slot] = False
                if isinstance(req, dict) and req.get("op") == "find" and out.get("pass") != "aged":
                    dt = self.clock() - t0  # the cost of one find (an aged request cost nothing; a card or
                    self.cost = 0.8 * self.cost + 0.2 * dt if self.cost else dt  # open is not a find)
                _send(conn, lock, out)
        finally:
            for v in views.values():
                v.close()

    def answer(self, req, t_in, views):
        """The reply to one request that arrived at t_in (views: this worker's open stores)."""
        rid, op = req.get("id"), req.get("op")
        try:
            budget = min(float(req.get("ms", MS)), self.max_ms)
        except (TypeError, ValueError):
            budget = MS
        deadline, now = t_in + budget / 1000.0, self.clock()
        if now >= deadline or (op == "find" and deadline - now < self.cost and self.q.qsize()):
            # its budget is gone, or (while others wait) less than one answer is left: the search would
            # come late, and the time it took would make the requests behind it late too
            self.counts["aged"] += 1
            return {"id": rid, "pass": "aged", "age": round((now - t_in) * 1000.0, 2)}
        try:
            if op == "find":
                out = {"hits": self._find(req, views, deadline)}
            elif op == "open":
                out = self._open(req, views)
            elif op == "card":
                out = self.card()
            else:
                raise ValueError("unknown op: %r" % (op,))
        except Late:
            self.counts["late"] += 1
            return {"id": rid, "pass": "late"}
        except Exception as e:  # a host fault is an answer too: no caller waits out its deadline, no worker dies
            self.counts["error"] += 1
            return {"id": rid, "error": "%s: %s" % (type(e).__name__, e)}
        self.counts[op] += 1
        out.update(id=rid, ms=round((self.clock() - t_in) * 1000.0, 2))
        return out

    def _view(self, views, name):
        if name not in views:
            from .view import View
            views[name] = View(self.stores[name][0])
        return views[name]

    def _find(self, req, views, deadline):
        q, k = str(req.get("q", "")), max(1, min(50, int(req.get("k", 8))))
        qw, hits = words(q), []
        for name in req.get("stores") or sorted(self.stores):
            if name not in self.stores:
                continue
            v = self._view(views, name)
            zoom = Zoom(v.store)  # one per request: a long-lived server keeps no text cache
            rows = search(v.index, q, k, self.stores[name][1] != "all", deadline, self.clock, NEIGHBOR)
            for rank, (cid, ref, score) in enumerate(rows):
                if self.clock() > deadline:
                    raise Late()
                c = Crumb.decode(v.store.get(cid))
                doc, a, b = parse_ref(ref)
                lines = get_lines(v.store, doc, a, b)
                best = max(lines, default=b"", key=lambda l: len(qw & words(l.decode("utf-8", "replace"))))
                hits.append({"store": name, "rank": rank, "id": cid, "ref": ref, "score": round(score, 3),
                             "kind": c.kind, "title": c.title[:40], "tokens": zoom.full(cid),
                             "line": best.decode("utf-8", "replace").strip()})
        return hits

    def _open(self, req, views):
        name = str(req["store"])
        v = self._view(views, name)
        blobs, withheld = collect(v.store, str(req["ref"]), self.stores[name][1] != "all")
        return {"store": name, "withheld": withheld,
                "blobs": {i: base64.b64encode(d).decode("ascii") for i, d in blobs.items()}}

    def card(self):
        return {"name": self.name, "stores": {n: s for n, (_, s) in self.stores.items()},
                "workers": self.workers, "depth": self.q.maxsize, "max_ms": self.max_ms,
                "cost_ms": round(self.cost * 1000.0, 2), "counts": dict(self.counts)}


class Peer:
    def __init__(self, name, addr, peer=""):
        host, _, port = addr.rpartition(":")
        self.name, self.addr, self.peer = name, (host or "127.0.0.1", int(port)), peer
        self.sock, self.buf, self.lag = None, b"", 0.002  # lag: round trip less host time (EWMA, s)
        self.cool = 0.0  # the host passed busy: no asks to it before this time

    def close(self):
        if self.sock is not None:
            _shut(self.sock)
        self.sock, self.buf = None, b""


class Hive:
    """Ask many peers at once. Every reply comes back by the deadline, or its peer passes. One caller
    at a time (the provider calls it under its lock)."""

    def __init__(self, peers, ms=MS, clock=time.monotonic):
        self.peers = {p.name: p for p in peers}
        self.ms, self.clock, self.n = float(ms), clock, 0

    @classmethod
    def from_config(cls, cfg):
        """cfg = {"ms": 150, "peers": {name: {"addr": "127.0.0.1:4731", "peer": "12D3..."}}}; None
        when it names no peer."""
        peers = [Peer(n, p["addr"], p.get("peer", "")) for n, p in sorted((cfg.get("peers") or {}).items())]
        return cls(peers, cfg.get("ms", MS)) if peers else None

    def close(self):
        for p in self.peers.values():
            p.close()

    def send(self, req, ms=None, names=None):
        """Send req to each peer (all, or names) at once; collect() gets the replies."""
        t0 = self.clock()
        self.n += 1
        pend = {"id": self.n, "t0": t0, "deadline": t0 + (self.ms if ms is None else float(ms)) / 1000.0,
                "wait": {}, "out": {}}
        for name in names or sorted(self.peers):
            p = self.peers.get(name)
            if p is None:
                pend["out"][name] = {"pass": "unknown"}
                continue
            left = pend["deadline"] - self.clock()
            if self.clock() < p.cool:
                pend["out"][name] = {"pass": "busy", "cool": True}
                continue
            try:
                if p.sock is None:
                    p.sock = socket.create_connection(p.addr, timeout=max(0.001, left))
                    p.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    p.sock.settimeout(1.0)
                msg = dict(req, id=self.n, ms=round(max(0.0, left - p.lag) * 1000.0, 2))
                p.sock.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                p.close()
                pend["out"][name] = {"pass": "down"}
                continue
            pend["wait"][name] = p
        return pend

    def collect(self, pend):
        """{peer name: reply}. Waits no longer than the deadline of the request."""
        out, wait = pend["out"], pend["wait"]
        sel = selectors.DefaultSelector()
        for p in wait.values():
            sel.register(p.sock, selectors.EVENT_READ, p)
        try:
            while wait:
                left = pend["deadline"] - self.clock()
                if left <= 0:
                    break
                for key, _ in sel.select(left):
                    p = key.data
                    try:
                        chunk = p.sock.recv(1 << 16)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(key.fileobj)
                        p.close()
                        out[p.name] = {"pass": "down"}
                        del wait[p.name]
                        continue
                    p.buf += chunk
                    while p.name in wait and b"\n" in p.buf:
                        line, p.buf = p.buf.split(b"\n", 1)
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(msg, dict) or msg.get("id") != pend["id"]:
                            continue  # the late reply to an earlier request
                        rt = self.clock() - pend["t0"]
                        if isinstance(msg.get("ms"), (int, float)):
                            p.lag = 0.8 * p.lag + 0.2 * max(0.0, rt - msg["ms"] / 1000.0)
                        msg["rt"] = round(rt * 1000.0, 2)
                        if msg.get("pass") == "busy" and isinstance(msg.get("wait"), (int, float)):
                            p.cool = self.clock() + min(float(msg["wait"]), 1000.0) / 1000.0
                        out[p.name] = msg
                        sel.unregister(key.fileobj)
                        del wait[p.name]
        finally:
            sel.close()
        for name in wait:
            out[name] = {"pass": "timeout"}
        return out

    def ask(self, req, ms=None, names=None):
        return self.collect(self.send(req, ms, names))

    def find(self, q, k=8, ms=None, names=None):
        return merge(self.ask({"op": "find", "q": q, "k": k}, ms, names), k)

    def fetch(self, name, store, ref, ms=None):
        """(zoom, reply): a Zoom over the blobs of ref that the peer sent, each one checked."""
        if name not in self.peers:
            raise KeyError("%s@%s/%s: no such peer" % (ref, name, store))
        r = self.ask({"op": "open", "store": store, "ref": ref}, max(self.ms, 1000.0) if ms is None else ms,
                     [name])[name]
        if "blobs" not in r:
            raise KeyError("%s@%s/%s: %s" % (ref, name, store, r.get("error") or "pass (%s)" % r.get("pass")))
        return Zoom(Received(verify(r["blobs"]))), r

    def open(self, name, store, ref, level=1.0, ms=None):
        """(text, reply): ref rendered from the blobs that the peer sent."""
        zoom, r = self.fetch(name, store, ref, ms)
        return zoom.render(ref, level), r


def split(ref):
    """"id@peer/store" -> (id, peer, store); a local id gives (id, "", "")."""
    ref, _, where = ref.strip().partition("@")
    peer, _, store = where.partition("/")
    return ref, peer, store


def merge(replies, k=8):
    """The best k hits of every peer by score (each names its peer). On LoCoMo split over two hosts,
    recall@10 was .589 merged by score and .549 with ranks interleaved; one store gave .603."""
    hits = [dict(h, peer=name) for name, r in replies.items() for h in r.get("hits") or ()]
    hits.sort(key=lambda h: (-h["score"], h["rank"]))
    return hits[:k]


def render(replies, k, ms, room=None):
    """(text, items): the hive part of a crumb_find result, one status line, then a handle and the
    best line of each hit while they fit in room; items = [(ref, level, bytes)] for the receipt."""
    def status(name, r):
        if "hits" in r:
            n = len(r["hits"])
            return "%s %d hit%s %.0f ms" % (name, n, "" if n == 1 else "s", r.get("rt", 0))
        return "%s pass (%s)" % (name, r.get("pass") or "error")
    head = "\u25c7 hive, budget %d ms: %s\n" % (ms, "; ".join(status(n, r) for n, r in sorted(replies.items())))
    out, items, used = [head], [], len(head.encode("utf-8"))
    for h in merge(replies, k):
        ref = "%s@%s/%s" % (h["id"], h["peer"], h["store"])
        part = "\u25b8 %s %s %s \u00b7%dt\n" % (ref, h["kind"], json.dumps(h["title"], ensure_ascii=False), h["tokens"])
        if h.get("line"):
            part += h["line"] + "\n"
        n = len(part.encode("utf-8"))
        if room is not None and used + n > room:
            break
        out.append(part)
        items.append((ref, 0.0, n))
        used += n
    return "".join(out), items
