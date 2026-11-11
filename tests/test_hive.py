import base64
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, hive
from breadcrumbs.crumbs import Crumb, put_crumb
from breadcrumbs.hermes import BreadcrumbsProvider
from breadcrumbs.history import Refs
from breadcrumbs.index import Index
from breadcrumbs.ingest import put_turn
from breadcrumbs.share import export_car, import_car
from breadcrumbs.store import Store
from breadcrumbs.zoom import Zoom


def make_store(root, turns):
    """A store folder with one turn crumb per (text, public); returns the crumb ids."""
    store, index = Store(os.path.join(root, "store")), Index(os.path.join(root, "index.db"))
    Refs(os.path.join(root, "refs"))
    ids = [put_turn(store, index, "user", text, "s%d" % i, ts=1000 + i, public=pub)
           for i, (text, pub) in enumerate(turns)]
    if len(ids) > 1:  # a public session over a public and a private turn
        ids.append(put_crumb(store, Crumb("sess", "two turns", kids=ids[:2], public=True)))
    store.flush()
    index.commit()
    index.close()
    store.close()
    return ids


class HiveData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)  # first in, last out: after every server and view closes (Windows)
        self.root = os.path.join(self.tmp.name, "a")
        self.pub, self.priv, self.both = make_store(self.root, [
            ("green tea steeps two minutes in the kettle water", True),
            ("the private kettle note stays home", False)])

    def serve(self, stores=None, **kw):
        s = hive.Server(stores or {"a": (self.root, "shared")}, port=0, **kw).start()
        self.addCleanup(s.close)
        return s

    def client(self, *servers, ms=1000):
        h = hive.Hive([hive.Peer("p%d" % i, "127.0.0.1:%d" % s.port) for i, s in enumerate(servers)], ms=ms)
        self.addCleanup(h.close)
        return h

    def views(self):
        views = {}
        self.addCleanup(lambda: [v.close() for v in views.values()])
        return views


class FindTest(HiveData):
    def test_a_shared_store_answers_with_its_public_crumbs_only(self):
        hits = self.client(self.serve()).find("kettle")
        self.assertEqual([h["id"] for h in hits], [self.pub])
        self.assertEqual((hits[0]["peer"], hits[0]["store"], hits[0]["kind"]), ("p0", "a", "turn"))
        self.assertEqual(hits[0]["line"], "green tea steeps two minutes in the kettle water")

    def test_a_store_served_whole_answers_with_every_crumb(self):
        hits = self.client(self.serve({"a": (self.root, "all")})).find("kettle")
        self.assertEqual(sorted(h["id"] for h in hits), sorted([self.pub, self.priv]))

    def test_a_request_that_breaks_the_search_gets_an_error_and_the_worker_lives_on(self):
        s = self.serve(workers=1)  # one worker: a dead worker would leave the host silent (each caller: timeout)
        real = s._find

        def broken(req, views, deadline):
            if req.get("q") == "boom":
                raise SyntaxError("a module of the host does not compile")
            return real(req, views, deadline)
        s._find = broken
        h = self.client(s)
        self.assertIn("SyntaxError", h.ask({"op": "find", "q": "boom"})["p0"].get("error", ""))
        self.assertEqual([x["id"] for x in h.find("kettle")], [self.pub])
        self.assertEqual(s.counts["error"], 1)

    def test_hits_merge_by_score_and_a_pass_adds_none(self):  # LoCoMo on 2 hosts, recall@10: .589 by score, .549 by rank
        replies = {"p0": {"hits": [{"id": "a", "rank": 0, "score": 9.0}, {"id": "b", "rank": 1, "score": 8.0}]},
                   "p1": {"hits": [{"id": "c", "rank": 0, "score": 3.0}]}, "p2": {"pass": "late"}}
        self.assertEqual([(h["id"], h["peer"]) for h in hive.merge(replies, 2)], [("a", "p0"), ("b", "p0")])

    def test_hits_of_two_peers_name_their_source(self):
        other = os.path.join(self.tmp.name, "b")
        b_pub = make_store(other, [("black tea likes kettle water off the boil", True)])[0]
        h = self.client(self.serve(), self.serve({"b": (other, "shared")}))
        hits = h.find("kettle")
        self.assertEqual(sorted((x["peer"], x["store"], x["id"]) for x in hits),
                         [("p0", "a", self.pub), ("p1", "b", b_pub)])
        self.assertEqual([x["rank"] for x in hits], [0, 0])

    def test_a_reply_carries_the_host_time_and_the_caller_measures_the_round_trip(self):
        r = self.client(self.serve()).ask({"op": "find", "q": "kettle"})["p0"]
        self.assertGreaterEqual(r["rt"], r["ms"])
        self.assertGreater(r["ms"], 0)


class PassTest(HiveData):
    def test_a_request_older_than_its_budget_passes_aged_without_a_search(self):
        s = self.serve()
        with mock.patch.object(s, "_find", side_effect=AssertionError("searched")):
            r = s.answer({"id": 7, "op": "find", "q": "kettle", "ms": 50}, time.monotonic() - 0.06, self.views())
        self.assertEqual((r["id"], r["pass"]), (7, "aged"))
        self.assertGreaterEqual(r["age"], 60)
        self.assertEqual(s.counts["aged"], 1)

    def test_a_host_caps_the_budget_it_was_given(self):
        s = self.serve(max_ms=40)
        r = s.answer({"id": 1, "op": "find", "q": "kettle", "ms": 10 ** 6}, time.monotonic() - 0.05, self.views())
        self.assertEqual(r["pass"], "aged")

    def test_a_search_that_runs_past_its_deadline_is_stopped_in_sqlite_and_passes_late(self):
        ticks = iter(range(10 ** 7))
        s = self.serve({"a": (self.root, "all")}, clock=lambda: next(ticks) / 1000.0)  # 1 ms per call
        with mock.patch.object(hive, "TICKS", 1):
            r = s.answer({"id": 2, "op": "find", "q": "kettle", "ms": 5}, 0.0, self.views())
        self.assertEqual(r["pass"], "late")
        self.assertEqual(s.counts["late"], 1)

    def test_the_deadline_guard_leaves_the_connection_usable(self):
        idx = Index(os.path.join(self.root, "index.db"), readonly=True)
        self.addCleanup(idx.close)
        with mock.patch.object(hive, "TICKS", 1), self.assertRaises(hive.Late):
            hive.search(idx, "kettle", 8, False, time.monotonic() - 1)
        self.assertEqual(len(hive.search(idx, "kettle", 8, False, time.monotonic() + 60)), 2)
        self.assertFalse(idx.db.in_transaction)

    def test_a_full_queue_passes_busy_at_once_and_the_queued_requests_still_run(self):
        s = self.serve(workers=1, depth=1)
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = s._find
        s._find = lambda *a: gate.wait(10) and real(*a)
        h1, h2, h3 = self.client(s), self.client(s), self.client(s)
        p1 = h1.send({"op": "find", "q": "kettle"}, ms=10000)  # the worker takes it and waits
        time.sleep(0.15)
        p2 = h2.send({"op": "find", "q": "kettle"}, ms=10000)  # it fills the queue
        time.sleep(0.15)
        t = time.monotonic()
        r3 = h3.ask({"op": "find", "q": "kettle"}, ms=10000)["p0"]
        self.assertEqual(r3["pass"], "busy")
        self.assertLess(time.monotonic() - t, 2.0)
        gate.set()
        self.assertEqual(h1.collect(p1)["p0"]["hits"][0]["id"], self.pub)
        self.assertEqual(h2.collect(p2)["p0"]["hits"][0]["id"], self.pub)

    def test_a_request_that_the_queue_would_make_late_passes_busy_at_arrival(self):
        s = self.serve(workers=1)  # the queue has room (depth 32); its wait does not
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = s._find
        s._find = lambda *a: gate.wait(10) and real(*a)
        h1, h2, h3 = self.client(s), self.client(s), self.client(s)
        p1 = h1.send({"op": "find", "q": "kettle"}, ms=10000)  # the one worker takes it and waits
        time.sleep(0.15)
        s.cost = 0.05  # the measured time of one answer: 50 ms
        r2 = h2.ask({"op": "find", "q": "kettle"}, ms=20)["p0"]
        self.assertEqual((r2.get("pass"), r2.get("wait")), ("busy", 50.0))
        p3 = h3.send({"op": "find", "q": "kettle"}, ms=10000)  # 50 ms fits in 10 s: it waits its turn
        gate.set()
        self.assertEqual(h1.collect(p1)["p0"]["hits"][0]["id"], self.pub)
        self.assertEqual(h3.collect(p3)["p0"]["hits"][0]["id"], self.pub)
        self.assertNotEqual(s.cost, 0.05)  # each answer updates the measured cost (here: up, the gate held p1)

    def test_a_caller_that_got_busy_asks_that_host_again_only_after_its_wait(self):
        s = self.serve(workers=1)  # Retry-After: a busy host is not flooded with asks it must refuse
        gate = threading.Event()
        self.addCleanup(gate.set)
        real = s._find
        s._find = lambda *a: gate.wait(10) and real(*a)
        h1, h2 = self.client(s), self.client(s)
        p1 = h1.send({"op": "find", "q": "kettle"}, ms=10000)
        time.sleep(0.15)
        s.cost = 0.05
        self.assertEqual(h2.ask({"op": "find", "q": "kettle"}, ms=20)["p0"]["pass"], "busy")  # wait 50 ms
        n = s.counts["busy"]
        r = h2.ask({"op": "find", "q": "kettle"}, ms=20)["p0"]  # within the 50 ms: not sent
        self.assertEqual((r["pass"], r.get("cool")), ("busy", True))
        self.assertEqual(s.counts["busy"], n)
        gate.set()
        h1.collect(p1)
        time.sleep(0.06)
        self.assertEqual(h2.find("kettle", ms=1000)[0]["id"], self.pub)

    def test_while_others_wait_a_worker_does_not_start_a_request_that_would_come_late(self):
        s = self.serve(workers=1)
        gate, ran = threading.Event(), []
        self.addCleanup(gate.set)
        real = s._find

        def gated(req, *a):
            ran.append(req.get("q"))
            return gate.wait(10) and real(req, *a)
        s._find = gated
        h1, h2, h3 = self.client(s), self.client(s), self.client(s)
        p1 = h1.send({"op": "find", "q": "kettle"}, ms=10000)
        time.sleep(0.1)
        s.cost = 0.3  # one answer: 300 ms
        p2 = h2.send({"op": "find", "q": "kettle short"}, ms=600)  # queued: a 300 ms wait fits in 600 ms
        p3 = h3.send({"op": "find", "q": "kettle long"}, ms=10000)
        time.sleep(0.35)  # p2 has about 250 ms left, less than one answer, and p3 waits behind it
        gate.set()
        self.assertEqual(h2.collect(p2)["p0"].get("pass"), "aged")
        self.assertEqual(h3.collect(p3)["p0"]["hits"][0]["id"], self.pub)
        h1.collect(p1)
        self.assertEqual(ran, ["kettle", "kettle long"])  # no search ran for p2

    def test_only_a_find_moves_the_measured_cost(self):
        s = self.serve(workers=1)  # the cost predicts the time of a find; a card takes almost no time
        h = self.client(s)
        s.cost = 0.05
        h.ask({"op": "card"}, ms=1000)
        self.assertEqual(s.cost, 0.05)
        h.find("kettle", ms=1000)
        self.assertNotEqual(s.cost, 0.05)

    def test_a_worker_opens_its_stores_before_the_first_request(self):
        s = hive.Server({"a": (self.root, "shared")}, port=0, workers=1)
        real = s._view

        def cold(views, name):
            if name not in views:
                time.sleep(0.3)  # a large store: its id index loads at open
            return real(views, name)
        s._view = cold
        s.start()
        self.addCleanup(s.close)
        time.sleep(0.6)
        self.assertEqual([h["id"] for h in self.client(s).find("kettle", ms=150)], [self.pub])

    def test_a_silent_peer_times_out_at_the_deadline_and_a_closed_port_is_down(self):
        silent = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(silent.close)
        gone = socket.create_server(("127.0.0.1", 0))
        port = gone.getsockname()[1]
        gone.close()
        h = hive.Hive([hive.Peer("quiet", "127.0.0.1:%d" % silent.getsockname()[1]),
                       hive.Peer("gone", "127.0.0.1:%d" % port)], ms=80)
        self.addCleanup(h.close)
        t = time.monotonic()
        r = h.ask({"op": "find", "q": "kettle"})
        took = time.monotonic() - t
        self.assertEqual((r["quiet"]["pass"], r["gone"]["pass"]), ("timeout", "down"))
        self.assertGreaterEqual(took, 0.075)
        self.assertLess(took, 0.6)

    def test_a_late_answer_to_an_old_request_is_skipped(self):
        s = self.serve(workers=1)
        gate = threading.Event()
        self.addCleanup(gate.set)
        real, calls = s._find, []
        s._find = lambda req, *a: (calls.append(req["q"]), len(calls) > 1 or gate.wait(10), real(req, *a))[2]
        h = self.client(s)
        self.assertEqual(h.ask({"op": "find", "q": "kettle"}, ms=100)["p0"]["pass"], "timeout")
        gate.set()  # the first answer now comes, after its caller gave up
        r = h.ask({"op": "find", "q": "green"}, ms=5000)["p0"]
        self.assertEqual([x["id"] for x in r["hits"]], [self.pub])
        self.assertEqual(calls, ["kettle", "green"])


class OpenTest(HiveData):
    def test_open_sends_exact_blobs_and_the_caller_renders_the_same_text(self):
        text, r = self.client(self.serve()).open("p0", "a", self.pub, 1.0)
        store = Store(os.path.join(self.root, "store"), readonly=True)
        self.addCleanup(store.close)
        self.assertEqual(text, Zoom(store).render(self.pub, 1.0))
        self.assertIn("green tea steeps two minutes in the kettle water", text)
        self.assertEqual(r["withheld"], 0)

    def test_a_private_crumb_is_refused_and_a_private_kid_is_withheld(self):
        h = self.client(self.serve())
        with self.assertRaises(KeyError):
            h.open("p0", "a", self.priv, 1.0)
        text, r = h.open("p0", "a", self.both, 1.0)
        self.assertEqual(r["withheld"], 1)
        self.assertIn("green tea", text)
        self.assertNotIn("private kettle", text)
        self.assertIn("withheld", text)

    def test_a_changed_blob_fails_its_crc32c_check(self):
        blobs = self.client(self.serve()).ask({"op": "open", "store": "a", "ref": self.pub})["p0"]["blobs"]
        hive.verify(blobs)
        bad = dict(blobs)
        k = next(i for i in bad if b"green" in base64.b64decode(bad[i]))
        bad[k] = base64.b64encode(base64.b64decode(bad[k]).replace(b"green", b"Green")).decode()
        with self.assertRaises(ValueError):
            hive.verify(bad)


class ImportTest(HiveData):
    def test_a_hive_peer_does_not_see_a_private_import(self):
        """The work of a child comes into its parent's store private: the parent serves its store shared."""
        car = os.path.join(self.tmp.name, "child.car")
        with Store(os.path.join(self.root, "store"), readonly=True) as child:
            export_car(child, [self.both], car)  # the session and its public turn; the private turn stays
        root = os.path.join(self.tmp.name, "parent")
        store, index = Store(os.path.join(root, "store")), Index(os.path.join(root, "index.db"))
        Refs(os.path.join(root, "refs"))
        imported = import_car(store, index, car, public=False)[0]
        index.close()
        store.close()
        shared, whole = (self.client(self.serve({"b": (root, scope)})) for scope in ("shared", "all"))
        self.assertEqual(shared.ask({"op": "find", "q": "kettle"})["p0"]["hits"], [])
        with self.assertRaises(KeyError):
            shared.open("p0", "b", imported, 1.0)
        self.assertEqual([h["kind"] for h in whole.find("kettle")], ["turn"])  # the crumbs are there
        self.assertIn("green tea steeps", whole.open("p0", "b", imported, 1.0)[0])


class ProviderTest(HiveData):
    def provider(self, port, ms=1000):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, "breadcrumbs"), exist_ok=True)
        with open(os.path.join(home, "breadcrumbs", "config.json"), "w") as f:
            json.dump({"hive": {"ms": ms, "peers": {"kali": {"addr": "127.0.0.1:%d" % port}}}}, f)
        p = BreadcrumbsProvider()
        p.initialize("s9", hermes_home=home, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        p.sync_turn("the kettle here is local", "noted", session_id="s9")
        return p

    def test_crumb_find_adds_the_hive_hits_and_crumb_opens_a_remote_id(self):
        p = self.provider(self.serve().port)
        out = json.loads(p.handle_tool_call("crumb_find", {"q": "kettle"}))["text"]
        self.assertIn("the kettle here is local", out)
        self.assertIn("%s@kali/a turn" % self.pub, out)
        self.assertIn("kali 1 hit", out)
        self.assertNotIn(self.priv, out)
        text = json.loads(p.handle_tool_call("mcp__crumb", {"id": "%s@kali/a" % self.pub, "level": 1}))["text"]
        self.assertIn("kali/a", text.splitlines()[0])
        self.assertIn("green tea steeps two minutes in the kettle water", text)
        self.assertIn("id@peer/store", p.get_tool_schemas()[0]["description"])

    def test_a_peer_that_is_down_passes_and_the_local_hits_stay(self):
        gone = socket.create_server(("127.0.0.1", 0))
        port = gone.getsockname()[1]
        gone.close()
        out = json.loads(self.provider(port).handle_tool_call("crumb_find", {"q": "kettle"}))["text"]
        self.assertIn("the kettle here is local", out)
        self.assertIn("kali pass (down)", out)

    def test_no_hive_config_keeps_the_tools_and_the_find_text(self):
        home = os.path.join(self.tmp.name, "plain")
        p = BreadcrumbsProvider()
        p.initialize("s1", hermes_home=home, platform="cli", agent_context="primary")
        self.addCleanup(p.shutdown)
        p.sync_turn("the kettle here is local", "noted", session_id="s1")
        self.assertIsNone(p.hive)
        self.assertNotIn("\u25c7", json.loads(p.handle_tool_call("crumb_find", {"q": "kettle"}))["text"])
        self.assertNotIn("id@peer/store", p.get_tool_schemas()[0]["description"])


class CliTest(HiveData):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(args))
        return code, out.getvalue()

    def home(self, port, **hive_cfg):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home, exist_ok=True)
        peer = {"addr": "127.0.0.1:%d" % port, "peer": "12D3KooWtest"}
        peer.update(hive_cfg.pop("peer", {}))
        with open(os.path.join(home, "config.json"), "w") as f:
            json.dump({"hive": dict(hive_cfg, peers={"kali": peer})}, f)
        return home

    def test_hive_find_and_open_print_remote_hits_and_text(self):
        home = self.home(self.serve().port)
        code, out = self.run_cli("--home", home, "hive", "find", "kettle", "--ms", "2000")
        self.assertEqual(code, 0)
        self.assertIn("%s@kali/a" % self.pub, out)
        code, out = self.run_cli("--home", home, "hive", "open", "%s@kali/a" % self.pub, "1")
        self.assertIn("green tea steeps two minutes", out)

    def test_hive_serve_takes_stores_and_their_scope(self):
        ap = cli.hive_stores(["a=" + self.root, "b=/x/y"], ["a"])
        self.assertEqual(ap, {"a": (self.root, "all"), "b": (os.path.abspath("/x/y"), "shared")})  # C:\x\y on Windows
        with self.assertRaises(SystemExit):
            cli.hive_stores(["nameonly"], [])

    @unittest.skipIf(os.name == "nt", "a shell script stands in for kubo")
    def test_hive_up_mounts_the_listeners_and_the_forwards_of_each_peer(self):
        home = self.home(4731, a2a=9900, peer={"a2a": "127.0.0.1:9931"})
        log = os.path.join(self.tmp.name, "ipfs.log")
        fake = os.path.join(self.tmp.name, "ipfs")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\n" % log)
        os.chmod(fake, 0o755)
        with mock.patch.dict(os.environ, {"BREADCRUMBS_IPFS": fake}):
            code, _ = self.run_cli("--home", home, "hive", "up", "--port", "4700")
        self.assertEqual(code, 0)
        with open(log) as f:
            calls = [line.split()[2:] for line in f]  # past "--timeout Ns"
        self.assertEqual(calls, [["p2p", "close", "-p", hive.PROTO],
                                 ["p2p", "listen", hive.PROTO, "/ip4/127.0.0.1/tcp/4700"],
                                 ["p2p", "close", "-p", hive.A2A],
                                 ["p2p", "listen", hive.A2A, "/ip4/127.0.0.1/tcp/9900"],
                                 ["p2p", "forward", hive.PROTO, "/ip4/127.0.0.1/tcp/4731", "/p2p/12D3KooWtest"],
                                 ["p2p", "forward", hive.A2A, "/ip4/127.0.0.1/tcp/9931", "/p2p/12D3KooWtest"]])


if __name__ == "__main__":
    unittest.main()
