"""The native Kitty/Ghostty explorer, breadcrumbs/spacegraph_kitty.py: its pure parts (the layouts, the semantic zoom,
the spring camera, the mouse parser, the Kitty escapes, the frame skip) and a headless run on the demo store.
No test needs a terminal, a network, Chrome or a model."""
import base64
import contextlib
import importlib.util
import io
import math
import os
import random
import re
import tempfile
import unittest
import zlib
from unittest import mock

os.environ.setdefault("BREADCRUMBS_SUMMARIZE", "0")  # unit tests never start the real model

from breadcrumbs import cli, kitty  # noqa: E402
from breadcrumbs import spacegraph_kitty as sk  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APC = re.compile(r"\x1b_G([^;]*);([^\x1b]*)\x1b\\")
_demo = {}


def store():
    """The demo store (demo/seed.py), made once for all tests of this file."""
    if "dir" not in _demo:
        spec = importlib.util.spec_from_file_location("demo_seed", os.path.join(ROOT, "demo", "seed.py"))
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        _demo["tmp"] = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            seed.main(_demo["tmp"].name)
        _demo["dir"] = os.path.join(_demo["tmp"].name, "breadcrumbs")
    return _demo["dir"]


def tearDownModule():
    if "tmp" in _demo:
        _demo["tmp"].cleanup()


class Clock:
    """A clock that moves only when a test moves it."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def read_png(png):
    """(width, height, raw rows with their filter byte) of an 8-bit RGB PNG that make_png wrote."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    i, idat, ihdr = 8, b"", None
    while i < len(png):
        n = int.from_bytes(png[i:i + 4], "big")
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + n]
        assert zlib.crc32(tag + data) & 0xffffffff == int.from_bytes(png[i + 8 + n:i + 12 + n], "big"), tag
        if tag == b"IHDR":
            ihdr = data
        elif tag == b"IDAT":
            idat += data
        i += 12 + n
    w, h = int.from_bytes(ihdr[:4], "big"), int.from_bytes(ihdr[4:8], "big")
    assert ihdr[8:] == bytes([8, 2, 0, 0, 0])
    return w, h, zlib.decompress(idat)


def frames_in(text):
    """The PNGs that a stream of Kitty graphics escapes draws, in order."""
    out, cur = [], []
    for keys, payload in APC.findall(text):
        cur.append(payload)
        if "m=0" in keys.split(","):
            out.append(base64.b64decode("".join(cur)))
            cur = []
    return out


def fake_nodes(n, seed=1):
    rng = random.Random(seed)
    roots = []
    for i in range(n):
        node = sk.Node("n%d" % i, {"pri": 0.3 + 0.7 * rng.random(), "tokens": 40 + int(300 * rng.random())})
        a, d = rng.random() * 2 * math.pi, (60 * math.sqrt(i + 1) + 80) * math.sqrt(rng.random())
        node.x, node.y = math.cos(a) * d, math.sin(a) * d
        node.s = 70 * (0.3 + 0.7 * node.pri)
        node.w = node.h = node.s
        node.tx, node.ty = node.x, node.y
        roots.append(node)
    return roots, rng


class AppCase(unittest.TestCase):
    def make_app(self, motion=True, size=(100, 30, 1000, 600), clock=None, out=None):
        """The explorer on the demo store, headless: 100 x 30 cells in a 1000 x 600 window, settled."""
        app = sk.SpaceGraphKitty(store(), motion=motion, seed=1, out=out)
        app.clock = clock or Clock()
        app.headless(*size)
        app.settle()
        self.addCleanup(app.view.close)
        return app


class Layouts(unittest.TestCase):
    def settle(self, seed):
        roots, rng = fake_nodes(12, seed)
        edges = [sk.Edge("n0", "n1", 0.6), sk.Edge("n1", "n2", 0.3), sk.Edge("n5", "n6", 1.0)]
        for e in edges:
            e.k = e.w
        temp, steps = 1.0, 0
        while temp >= 0.004:
            temp = sk.force_step(roots, edges, temp, rng)
            steps += 1
            self.assertLess(steps, 2000, "the temperature must fall")
        return roots, steps

    def test_the_force_layout_settles(self):
        roots, steps = self.settle(1)
        self.assertLess(steps, 800)  # 0.992 a step: from 1 to 0.004 in about 690 steps
        again = [(n.x, n.y) for n in roots]
        rng = random.Random(9)
        self.assertEqual(sk.force_step(roots, [], 0.003, rng), 0.003)  # below 0.004 it does nothing
        self.assertEqual([(n.x, n.y) for n in roots], again)

    def test_the_force_layout_is_the_same_for_the_same_seed_and_not_for_another(self):
        a, _ = self.settle(1)
        b, _ = self.settle(1)
        c, _ = self.settle(2)
        self.assertEqual([(n.x, n.y) for n in a], [(n.x, n.y) for n in b])
        self.assertNotEqual([(n.x, n.y) for n in a], [(n.x, n.y) for n in c])

    def test_boxes_do_not_overlap_when_the_force_layout_has_settled(self):
        roots, _ = self.settle(3)
        for i, a in enumerate(roots):
            for b in roots[i + 1:]:
                self.assertGreater(math.hypot(a.x - b.x, a.y - b.y), a.s * 0.72 + b.s * 0.72, (a.key, b.key))

    def test_a_link_pulls_two_boxes_nearer_than_no_link(self):
        def gap(link):
            roots, rng = fake_nodes(6, 4)
            edges = [sk.Edge("n0", "n1", 1.0)] if link else []
            temp = 1.0
            while temp >= 0.004:
                temp = sk.force_step(roots, edges, temp, rng)
            return math.hypot(roots[0].x - roots[1].x, roots[0].y - roots[1].y)
        self.assertLess(gap(True), gap(False))

    def test_squarify_tiles_the_rectangle_with_no_overlap_and_no_negative_cell(self):
        W, H = 400.0, 300.0
        vals = [100, 80, 60, 40, 30, 20, 10]
        cells = [{"v": v * W * H / sum(vals)} for v in vals]
        sk.squarify(cells, 0.0, 0.0, W, H)
        self.assertAlmostEqual(sum(c["w"] * c["h"] for c in cells), W * H, places=6)
        for c in cells:
            self.assertGreater(c["w"], 0)
            self.assertGreater(c["h"], 0)
            self.assertGreaterEqual(c["x"], -1e-9)
            self.assertGreaterEqual(c["y"], -1e-9)
            self.assertLessEqual(c["x"] + c["w"], W + 1e-6)
            self.assertLessEqual(c["y"] + c["h"], H + 1e-6)
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
                oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
                self.assertFalse(ox > 1e-6 and oy > 1e-6)
        self.assertGreater(cells[0]["w"] * cells[0]["h"], cells[-1]["w"] * cells[-1]["h"])  # the biggest value, the biggest cell

    def test_every_fixed_layout_gives_each_box_a_finite_place_and_none_on_another(self):
        for layout in ("grid", "treemap", "tree", "radial"):
            roots, _ = fake_nodes(9, 5)
            edges = [sk.Edge("n0", "n1", 1.0), sk.Edge("n1", "n2", 0.5), sk.Edge("n3", "n4", 0.7)]
            for e in edges:
                e.k = e.w
            sk.arrange(roots, edges, layout, 16 / 9)
            for n in roots:
                self.assertTrue(all(math.isfinite(v) for v in (n.tx, n.ty, n.tw, n.th)), (layout, n.key))
            for i, a in enumerate(roots):
                for b in roots[i + 1:]:
                    self.assertGreater(math.hypot(a.tx - b.tx, a.ty - b.ty), 1.0, (layout, a.key, b.key))
            self.assertAlmostEqual((min(n.tx for n in roots) + max(n.tx for n in roots)) / 2, 0.0, places=6)  # centred

    def test_the_grid_keeps_the_order_and_the_treemap_gives_a_bigger_session_a_bigger_cell(self):
        roots, _ = fake_nodes(6, 6)
        roots[0].tokens, roots[1].tokens = 100000, 10
        roots[0].s = roots[1].s = 60.0
        sk.arrange(roots, [], "treemap", 1.5)
        self.assertGreater(roots[0].tw * roots[0].th, roots[1].tw * roots[1].th)
        sk.arrange(roots, [], "grid", 1.5)
        self.assertEqual((roots[0].tw, roots[0].th), (0.0, 0.0))  # the grid has no cell size
        self.assertLessEqual(roots[0].ty, roots[-1].ty)  # the first session sits in the first row

    def test_a_tree_hangs_the_linked_sessions_from_a_hub_and_marks_the_tree_links(self):
        roots, _ = fake_nodes(5, 7)
        edges = [sk.Edge("n0", "n1", 1.0), sk.Edge("n1", "n2", 0.5), sk.Edge("n1", "n3", 0.4), sk.Edge("n0", "n3", 0.1)]
        trees = sk.forest(roots, edges)
        self.assertEqual([t.many for t in trees], [4, 1])  # the biggest group first, the loose session last
        self.assertEqual(trees[0].key, "n1")  # the hub: the fewest hops to the farthest session
        self.assertEqual(sum(1 for e in edges if e.tree), 3)  # a link to each of the other three sessions; the weak one is not a tree link


class SemanticZoom(AppCase):
    def test_the_level_is_the_biggest_one_whose_text_fits_the_area(self):
        self.assertEqual(sk.pick(1000, 0), 0)
        self.assertEqual(sk.pick(1000, 100), 2 ** -5)   # 4000 chars * 2**-5 = 125 <= 150 < 4000 * 2**-4
        self.assertEqual(sk.pick(1000, 4000 / 1.5), 1)  # the whole text fits
        self.assertEqual(sk.pick(10, 400), 1)
        self.assertEqual(sk.pick(0, 10), 1)   # a crumb of no tokens counts as one token: 4 characters fit

    def test_a_bigger_area_never_picks_a_smaller_level(self):
        for tokens in (3, 40, 223, 5000):
            levels = [sk.pick(tokens, chars) for chars in range(0, 20000, 7)]
            self.assertEqual(levels, sorted(levels), tokens)
            self.assertEqual(levels[-1], 1)

    def test_the_levels_are_the_pages(self):
        self.assertEqual(sk.LEVELS[0], 0)
        self.assertEqual(sk.LEVELS[1], 2 ** -12)
        self.assertEqual(sk.LEVELS[-2:], [2 ** -1, 1])
        self.assertEqual(len(sk.LEVELS), 14)

    def test_the_text_of_a_crumb_as_words(self):
        raw = '\u25b8 509698e6 sess \u00b722t\n  \u25b8 8997040f turn "user: My green tea" \u00b722t\nMy green tea is bitter.\n  \u22ef 1234abcd 5 more'
        # its own handle line goes; the handle of a part keeps its kind and title; a gap is an ellipsis
        self.assertEqual(sk.tidy(raw, True), "\u25b8 turn user: My green tea\nMy green tea is bitter.\n  \u2026")
        self.assertEqual(sk.flat(raw), "My green tea is bitter.")
        parts = '\u25b8 00000000 sess \u00b79t\n  \u25b8 aaaaaaaa turn "first" \u00b73t\n  \u25b8 bbbbbbbb turn "second" \u00b73t'
        self.assertEqual(sk.flat(parts), "first \u00b7 second")   # a crumb of handles only: the titles of its parts

    def test_long_lines_wrap_at_a_space_and_a_handle_line_is_left_whole(self):
        self.assertEqual(sk.wrap("the kettle boils too fast", 10), ["the kettle", "boils too", "fast"])
        self.assertEqual(sk.wrap("abcdefghijklmnop", 6), ["abcdef", "ghijkl", "mnop"])
        handle = "\u25b8 509698e6 sess \u00b722t | " + "word " * 20
        self.assertEqual(sk.wrap(handle, 10), [handle])

    def test_on_the_demo_store_a_box_shows_more_of_a_crumb_as_it_grows(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        levels = []
        for cols_rows in (30, 300, 3000):
            level = sk.pick(today.tokens, cols_rows)
            levels.append(level)
            app.text(today, level)
        app.drain()
        self.assertEqual(levels, sorted(levels))
        self.assertLess(levels[0], levels[-1])
        low, high = app.text(today, levels[0]), app.text(today, levels[-1])
        self.assertLess(len(low), len(high))
        self.assertIn("My green tea is bitter again. What did we say about the kettle?", high)  # the exact line
        self.assertNotIn("My green tea is bitter again. What did we say about the kettle?", low)

    def test_the_dive_from_all_sessions_to_a_session_to_a_turn_ends_in_the_exact_lines(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        app.focus(today.key)
        app.settle()
        self.assertEqual(today.doc.state, "ready")
        frame = next(n for n in today.doc.frames if "bike" in n.tip)
        app.focus(frame.key)
        app.settle()
        self.assertEqual(app.stack, ["*", today.key, frame.key])
        cards = [n for n in today.doc.cards if n.kind == "prompt" and "bike" in n.head]
        self.assertTrue(cards)
        # the card of the prompt now holds the exact line, not the cue words of the overview
        app.paint()
        text = app.text(cards[0], 1) or ""
        self.assertIn("And the bike: the chain skips again on the small cog.", text)


class Spring(unittest.TestCase):
    DTS = (0.001, 0.004, 1 / 60, 1 / 30, 0.1, 0.25, 0.5)

    def run_spring(self, omega, dt, total=4.0, x=0.0, v=0.0, goal=100.0):
        path = []
        for _ in range(int(round(total / dt))):
            x, v = sk.damp(x, v, goal, omega, dt)
            path.append((x, v))
        return path

    def test_the_two_omegas_are_7_for_a_focus_move_and_14_after_a_wheel_step(self):
        self.assertEqual((sk.OMEGA_FOCUS, sk.OMEGA_WHEEL), (7.0, 14.0))

    def test_from_rest_it_never_passes_the_goal_and_never_turns_back(self):
        for omega in (sk.OMEGA_FOCUS, sk.OMEGA_WHEEL):
            for dt in self.DTS:
                path = self.run_spring(omega, dt)
                xs = [p[0] for p in path]
                self.assertLessEqual(max(xs), 100.0, (omega, dt))
                self.assertEqual(xs, sorted(xs), (omega, dt))   # always moving toward the goal
                self.assertTrue(all(p[1] >= -1e-9 for p in path), (omega, dt))

    def test_from_rest_toward_a_goal_below_it_never_passes_either(self):
        for omega in (sk.OMEGA_FOCUS, sk.OMEGA_WHEEL):
            path = self.run_spring(omega, 1 / 30, x=100.0, goal=-50.0)
            self.assertGreaterEqual(min(p[0] for p in path), -50.0)

    def test_it_reaches_the_goal_and_stops(self):
        for omega in (sk.OMEGA_FOCUS, sk.OMEGA_WHEEL):
            for dt in self.DTS:
                x, v = self.run_spring(omega, dt, total=6.0)[-1]
                self.assertAlmostEqual(x, 100.0, delta=1e-4, msg=(omega, dt))
                self.assertAlmostEqual(v, 0.0, delta=1e-3, msg=(omega, dt))

    def test_it_is_exact_so_every_frame_time_from_1_ms_to_500_ms_gives_the_same_state(self):
        ref = self.run_spring(sk.OMEGA_FOCUS, 0.001, total=2.0)[-1]
        for dt in self.DTS[1:]:
            got = self.run_spring(sk.OMEGA_FOCUS, dt, total=2.0)[-1]
            self.assertAlmostEqual(got[0], ref[0], places=7, msg=dt)
            self.assertAlmostEqual(got[1], ref[1], places=6, msg=dt)

    def test_it_starts_slow_runs_and_stops_slow_with_no_jump_and_no_dead_stop(self):
        path = self.run_spring(sk.OMEGA_FOCUS, 0.001, total=3.0)
        speeds = [p[1] for p in path]
        peak = speeds.index(max(speeds))
        self.assertAlmostEqual((peak + 1) * 0.001, 1 / sk.OMEGA_FOCUS, delta=0.005)  # the speed peaks at 1/omega
        self.assertLess(speeds[0], 0.025 * max(speeds))    # a slow start: 1 ms in, 2% of the top speed
        self.assertLess(speeds[-1], 0.001 * max(speeds))   # a slow stop
        self.assertEqual(speeds[peak:], sorted(speeds[peak:], reverse=True))  # after the peak it only slows
        lerp_first = 100.0 * (1 - math.exp(-6 / 60))        # the page's first-order camera moves this far in the first frame
        self.assertLess(self.run_spring(sk.OMEGA_FOCUS, 1 / 60)[0][0], lerp_first / 5)

    def test_the_wheel_spring_is_quicker_than_the_focus_spring(self):
        def t90(omega):
            path = self.run_spring(omega, 0.001)
            return next(i for i, p in enumerate(path) if p[0] >= 90.0)
        self.assertLess(t90(sk.OMEGA_WHEEL), t90(sk.OMEGA_FOCUS) * 0.6)

    def test_a_speed_that_would_carry_it_past_the_goal_is_cut_so_it_just_arrives(self):
        x, goal, omega = 0.0, 10.0, sk.OMEGA_FOCUS
        v = sk.keep_inside(x, 1000.0, goal, omega)
        self.assertEqual(v, omega * (goal - x))
        self.assertEqual(sk.keep_inside(x, 5.0, goal, omega), 5.0)    # a slow speed stays
        self.assertEqual(sk.keep_inside(x, -50.0, goal, omega), -50.0)  # a speed away from the goal stays
        for start, kept in ((1000.0, True), (v, False)):
            xs = [p[0] for p in self.run_spring(omega, 0.01, total=4.0, x=x, v=start, goal=goal)]
            self.assertEqual(max(xs) > goal + 1e-9, kept)


class CameraTest(unittest.TestCase):
    MID = (500.0, 300.0)

    def screen(self, cam, wx, wy):
        return (wx - cam.x) * cam.k + self.MID[0], (wy - cam.y) * cam.k + self.MID[1]

    def test_the_wheel_moves_only_the_target_and_the_point_under_the_pointer_stays_under_it(self):
        cam = sk.Camera()
        cam.tx, cam.ty, cam.tk = 10.0, 20.0, 2.0
        cam.snap()
        px, py = 320.0, 140.0
        wx, wy = (px - self.MID[0]) / cam.k + cam.x, (py - self.MID[1]) / cam.k + cam.y
        cam.zoom_at(px, py, 3, *self.MID)
        self.assertEqual((cam.x, cam.y, cam.k), (10.0, 20.0, 2.0))  # nothing moved yet
        self.assertAlmostEqual(cam.tk, 2.0 * math.exp(3 * sk.WHEEL_STEP))
        self.assertEqual(cam.omega, sk.OMEGA_WHEEL)
        for i in range(120):
            cam.step(1 / 60, *self.MID)
            sx, sy = self.screen(cam, wx, wy)
            self.assertAlmostEqual(sx, px, places=6, msg=i)
            self.assertAlmostEqual(sy, py, places=6, msg=i)
        self.assertAlmostEqual(cam.k, cam.tk)
        self.assertFalse(cam.moving())
        self.assertAlmostEqual(self.screen(cam, wx, wy)[0], px, places=6)

    def test_quick_wheel_steps_add_up_because_each_starts_from_the_target(self):
        cam = sk.Camera()
        cam.snap()
        for _ in range(5):
            cam.zoom_at(*self.MID, 1, *self.MID)
            cam.step(1 / 100, *self.MID)
        self.assertAlmostEqual(cam.tk, 0.5 * math.exp(5 * sk.WHEEL_STEP))
        self.assertLess(cam.k, cam.tk)   # the camera is still on its way
        self.assertGreater(cam.k, 0.5)

    def test_the_zoom_is_kept_inside_its_limits(self):
        cam = sk.Camera()
        for _ in range(400):
            cam.zoom_at(*self.MID, 10, *self.MID)
        self.assertEqual(cam.tk, sk.K_MAX)
        for _ in range(400):
            cam.zoom_at(*self.MID, -10, *self.MID)
        self.assertEqual(cam.tk, sk.K_MIN)

    def test_a_focus_move_uses_omega_7_and_arrives_without_passing_the_goal(self):
        cam = sk.Camera()
        cam.snap()
        cam.aim(300.0, -200.0, 4.0)
        self.assertEqual(cam.omega, sk.OMEGA_FOCUS)
        xs, ks = [], []
        for _ in range(240):
            cam.step(1 / 60, *self.MID)
            xs.append(cam.x)
            ks.append(cam.k)
        self.assertLessEqual(max(xs), 300.0 + 1e-9)
        self.assertLessEqual(max(ks), 4.0 + 1e-9)
        self.assertEqual((cam.x, cam.y, cam.k), (300.0, -200.0, 4.0))
        self.assertFalse(cam.moving())

    def test_a_new_target_in_mid_flight_keeps_the_speed_but_does_not_bounce(self):
        cam = sk.Camera()
        cam.snap()
        cam.aim(100.0, 0.0, 0.5)
        for _ in range(8):
            cam.step(1 / 60, *self.MID)
        cam.aim(cam.x + 1.0, 0.0, 0.5)   # a goal just ahead of a fast camera
        xs = []
        for _ in range(120):
            cam.step(1 / 60, *self.MID)
            xs.append(cam.x)
        self.assertLessEqual(max(xs), cam.tx + 1e-9)

    def test_a_drag_follows_the_pointer_one_to_one_and_a_press_stops_a_glide(self):
        cam = sk.Camera()
        cam.snap()
        cam.drag(-50.0, 12.5)
        self.assertEqual((cam.x, cam.y, cam.tx, cam.ty), (-50.0, 12.5, -50.0, 12.5))
        cam.throw(400.0, -100.0)
        self.assertTrue(cam.gliding)
        cam.step(1 / 60, *self.MID)
        cam.grab()
        self.assertEqual((cam.vx, cam.vy, cam.gliding), (0.0, 0.0, False))
        self.assertEqual((cam.tx, cam.ty), (cam.x, cam.y))
        before = (cam.x, cam.y)
        for _ in range(30):
            cam.step(1 / 60, *self.MID)
        self.assertEqual((cam.x, cam.y), before)

    def test_a_throw_keeps_the_speed_of_the_hand_aims_at_speed_times_0_35_and_stops_there(self):
        cam = sk.Camera()
        cam.snap()
        cam.throw(300.0, -60.0)
        self.assertAlmostEqual(cam.tx, 300.0 * 0.35)
        self.assertAlmostEqual(cam.ty, -60.0 * 0.35)
        self.assertEqual(cam.omega, sk.OMEGA_FOCUS)
        cam.step(0.001, *self.MID)
        self.assertAlmostEqual(cam.x / 0.001, 300.0, delta=5.0)   # it leaves at the speed of the hand
        peak, xs = 0.0, []
        for _ in range(600):
            cam.step(1 / 60, *self.MID)
            xs.append(cam.x)
            peak = max(peak, cam.vx)
        self.assertLessEqual(max(xs), cam.tx + 1e-9)               # never past the target
        self.assertAlmostEqual(cam.x, 105.0)
        self.assertLess(peak, 300.0 * 1.15)                         # no kick after the release
        self.assertFalse(cam.moving())

    def test_the_hand_speed_is_the_speed_of_the_last_80_ms(self):
        d = sk.Drag(0, 0, 0.0, 0.0, 0.0)
        for i in range(1, 11):          # 1000 px a second for 100 ms
            d.add(0.01 * i, 10 * i, 0)
        vx, vy = d.speed(0.10)
        self.assertAlmostEqual(vx, 1000.0, delta=1.0)
        self.assertEqual(vy, 0.0)
        self.assertEqual(d.speed(0.30), (0.0, 0.0))   # the hand had stopped for 200 ms: no glide
        self.assertAlmostEqual(d.speed(0.13)[0], 1000.0, delta=1.0)   # let go 30 ms after the last move: still a throw
        self.assertEqual(d.speed(0.15), (0.0, 0.0))                   # 50 ms after: the hand had stopped
        stopped = sk.Drag(0, 0, 0.0, 0.0, 0.0)
        stopped.add(0.5, 1, 0)   # one sample in the last 80 ms: no speed
        self.assertEqual(stopped.speed(0.5), (0.0, 0.0))

    def test_motion_off_is_the_camera_of_the_old_page(self):
        cam = sk.Camera(motion=False)
        cam.snap()
        cam.zoom_at(*self.MID, 2, *self.MID)
        self.assertAlmostEqual(cam.k, cam.tk)                       # the wheel jumps
        self.assertAlmostEqual(cam.k, 0.5 * math.exp(2 * sk.WHEEL_STEP))
        cam.aim(100.0, 0.0, 1.0)
        cam.step(1 / 60, *self.MID)
        self.assertAlmostEqual(cam.x, 100.0 * (1 - math.exp(-6 / 60)))  # a share of the way: full speed at the start
        self.assertEqual((cam.vx, cam.vy, cam.vk), (0.0, 0.0, 0.0))
        for _ in range(300):
            cam.step(1 / 60, *self.MID)
        self.assertEqual((cam.x, cam.y), (100.0, 0.0))

    def test_the_motion_setting(self):
        self.assertTrue(sk.motion_on(None, {}))
        self.assertFalse(sk.motion_on("0", {}))
        self.assertFalse(sk.motion_on(None, {"BREADCRUMBS_MOTION": "0"}))
        self.assertTrue(sk.motion_on("1", {"BREADCRUMBS_MOTION": "0"}))   # the flag wins


class Input(unittest.TestCase):
    def feed(self, *chunks):
        inp, out = sk.Input(), []
        for c in chunks:
            out += inp.feed(c)
        return out

    def test_sgr_mouse_press_release_drag_and_wheel(self):
        evs = self.feed(b"\x1b[<0;10;5M\x1b[<32;11;6M\x1b[<0;12;7m\x1b[<64;3;4M\x1b[<65;3;4M\x1b[<35;7;8M\x1b[<2;1;1M")
        got = [(e.kind, e.action, e.x, e.y, e.button, e.dy) for e in evs]
        self.assertEqual(got, [("mouse", "press", 10, 5, 0, 0), ("mouse", "move", 11, 6, 0, 0), ("mouse", "release", 12, 7, 0, 0),
                               ("mouse", "wheel", 3, 4, 0, 1), ("mouse", "wheel", 3, 4, 1, -1), ("mouse", "move", 7, 8, 3, 0),
                               ("mouse", "press", 1, 1, 2, 0)])

    def test_mouse_modifiers_and_pixel_positions(self):
        e = self.feed(b"\x1b[<4;1234;567M")[0]    # shift
        self.assertEqual((e.x, e.y, e.mods), (1234, 567, sk.SHIFT))
        e = self.feed(b"\x1b[<16;1;1M")[0]
        self.assertEqual(e.mods, sk.CTRL)
        e = self.feed(b"\x1b[<68;5;5M")[0]        # shift + wheel up
        self.assertEqual((e.action, e.mods, e.dy), ("wheel", sk.SHIFT, 1))

    def test_a_report_that_arrives_in_pieces_is_parsed_once_it_is_whole(self):
        inp = sk.Input()
        self.assertEqual(inp.feed(b"\x1b[<0;1"), [])
        self.assertTrue(inp.pending())
        self.assertEqual(inp.feed(b"2;5"), [])
        evs = inp.feed(b"M")
        self.assertEqual([(e.action, e.x, e.y) for e in evs], [("press", 12, 5)])
        self.assertFalse(inp.pending())

    def test_a_lone_escape_waits_and_is_the_escape_key_when_nothing_follows(self):
        inp = sk.Input()
        self.assertEqual(inp.feed(b"\x1b"), [])
        self.assertEqual([e.name for e in inp.flush()], ["esc"])
        self.assertEqual([e.name for e in self.feed(b"\x1b\x1b[A")], ["esc", "up"])

    def test_keys(self):
        evs = self.feed(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1bOA\x1b[H\x1b[F\x1b[5~\x1b[6~\x1b[3~\x1b[Z")
        self.assertEqual([e.name for e in evs], ["up", "down", "right", "left", "up", "home", "end", "pgup", "pgdn", "delete", "shift-tab"])
        e = self.feed(b"\x1b[1;2A")[0]
        self.assertEqual((e.name, e.mods), ("up", sk.SHIFT))
        e = self.feed(b"\x1b[1;5C")[0]
        self.assertEqual((e.name, e.mods), ("right", sk.CTRL))
        evs = self.feed(b"q\r\x7f\t \x03a")
        self.assertEqual([(e.kind, e.name or e.text) for e in evs],
                         [("char", "q"), ("key", "enter"), ("key", "backspace"), ("key", "tab"), ("key", "space"), ("key", "ctrl-c"), ("char", "a")])

    def test_text_that_is_not_ascii_arrives_whole_even_when_it_is_split(self):
        data = "\u00e9\u2192\U0001f434".encode("utf-8")
        for cut in range(1, len(data)):
            evs = self.feed(data[:cut], data[cut:])
            self.assertEqual("".join(e.text for e in evs), "\u00e9\u2192\U0001f434", cut)

    def test_the_answer_to_the_question_about_pixel_reports(self):
        e = self.feed(b"\x1b[?1016;1$y")[0]
        self.assertEqual((e.kind, e.name, e.value), ("mode", "1016", 1))
        self.assertEqual(self.feed(b"\x1b[?1016;0$y")[0].value, 0)

    def test_a_sequence_that_the_program_does_not_know_is_dropped_whole(self):
        self.assertEqual(self.feed(b"\x1b[200~x"), [sk.Ev("char", text="x")])
        self.assertEqual(self.feed(b"\x1b[38;5;196m\x1b[?2004h"), [])


class KittyOutput(unittest.TestCase):
    def test_a_big_png_goes_in_chunks_of_4096_and_decodes_to_the_same_bytes(self):
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 50
        cmds = APC.findall(sk.kg_image(png, cols=100, rows=30))
        self.assertEqual(len(cmds), 5)
        keys = dict(kv.split("=") for kv in cmds[0][0].split(","))
        self.assertEqual(keys, {"a": "T", "f": "100", "q": "2", "i": "1", "p": "1", "C": "1", "c": "100", "r": "30", "m": "1"})
        self.assertEqual([k for k, _ in cmds[1:]], ["m=1", "m=1", "m=1", "m=0"])   # only the first chunk has the keys
        self.assertTrue(all(len(p) <= 4096 for _, p in cmds))
        self.assertEqual(base64.b64decode("".join(p for _, p in cmds)), png)

    def test_the_same_image_and_placement_every_frame_so_the_next_frame_replaces_the_last(self):
        a, b = sk.kg_frame(b"one", 80, 24), sk.kg_frame(b"two", 80, 24)
        self.assertTrue(a.startswith("\x1b[H\x1b_G") and b.startswith("\x1b[H\x1b_G"))
        self.assertEqual(APC.findall(a)[0][0], APC.findall(b)[0][0])
        self.assertIn(",i=1,p=1,", APC.findall(a)[0][0])

    def test_it_agrees_with_kitty_py_where_it_can(self):
        png = b"\x89PNGxx" * 1000
        self.assertEqual(sk.kg_image(png, cols=90).replace(",r=", ","), sk.kg_image(png, cols=90))   # no rows: no r key
        self.assertEqual(sk.kg_image(png, cols=90), kitty.image(png, cols=90))
        self.assertEqual(sk.kg_clear(), kitty.clear())

    def test_the_terminal_is_set_up_for_the_mouse_and_given_back(self):
        on, off = sk.enter_codes(), sk.leave_codes()
        for code in ("\x1b[?1049h", "\x1b[?25l", "\x1b[?1002h", "\x1b[?1006h"):
            self.assertIn(code, on)
        self.assertNotIn("\x1b[?1003h", sk.enter_codes(hover=False))
        for code in ("\x1b[?1049l", "\x1b[?25h", "\x1b[?1002l", "\x1b[?1006l", "\x1b[?1003l", "\x1b[?1016l"):
            self.assertIn(code, off)
        self.assertTrue(off.startswith(kitty.clear()))             # the image is deleted first
        self.assertTrue(off.endswith("\x1b[?1049l"))               # the old screen comes back last
        self.assertLess(off.index("\x1b[?25h"), off.index("\x1b[?1049l"))
        for mode in ("1049", "1002", "1003", "1006", "1016"):      # every mode that was set is reset
            self.assertIn("\x1b[?%sh" % mode, on)
            self.assertIn("\x1b[?%sl" % mode, off)

    def test_closing_the_terminal_writes_the_way_back_and_restores_the_line_settings(self):
        out = io.StringIO()
        app = sk.SpaceGraphKitty(store(), out=out)
        self.assertEqual(out.getvalue(), "")
        app.opened, app.fd_in, app.old_termios, app.old_handlers = True, 0, ["saved"], {}
        with mock.patch.object(sk.termios, "tcsetattr") as tcsetattr:
            app.close_terminal()
            app.close_terminal()   # a second call does nothing
        self.assertEqual(out.getvalue(), sk.leave_codes())
        tcsetattr.assert_called_once_with(0, sk.termios.TCSAFLUSH, ["saved"])

    def test_a_closed_window_ends_the_program_without_a_traceback(self):
        class Gone(io.StringIO):
            def write(self, s):
                raise OSError(5, "Input/output error")   # what a pty gives when its window is closed
        app = sk.SpaceGraphKitty(store(), out=Gone())
        app.running = True
        app.write("one frame")                  # no exception
        self.assertTrue(app.gone)
        self.assertFalse(app.running)           # the loop ends
        app.write("another")                    # nothing more is tried
        app.opened, app.fd_in, app.old_termios, app.old_handlers = True, 0, ["saved"], {}
        with mock.patch.object(sk.termios, "tcsetattr", side_effect=sk.termios.error(5, "Input/output error")):
            app.close_terminal()                # the line settings of a gone terminal cannot be restored: no exception
        self.assertFalse(app.opened)


class Pictures(unittest.TestCase):
    def test_a_png_is_what_the_terminal_reads_and_the_buffer_is_its_raw_rows(self):
        cv = sk.Canvas(37, 11)
        cv.rect(2, 3, 5, 4, sk.ACCENT)
        w, h, raw = read_png(cv.png())
        self.assertEqual((w, h), (37, 11))
        self.assertEqual(raw, bytes(cv.buf))
        rows = [raw[i * (3 * w + 1):(i + 1) * (3 * w + 1)] for i in range(h)]
        self.assertTrue(all(r[0] == 0 for r in rows))   # filter type 0 on every row
        self.assertEqual(rows[3][1 + 3 * 2:1 + 3 * 3], bytes(sk.ACCENT))

    def test_shapes_clip_to_the_picture_and_to_the_clip_rectangle(self):
        cv = sk.Canvas(20, 20)
        cv.rect(-10, -10, 100, 100, (1, 2, 3))
        self.assertEqual({cv.at(x, y) for x in (0, 19) for y in (0, 19)}, {(1, 2, 3)})
        cv.set_clip(5, 5, 10, 10)
        cv.rect(0, 0, 20, 20, (9, 9, 9))
        cv.line(0, 0, 19, 19, (7, 7, 7))
        self.assertEqual(cv.at(4, 4), (1, 2, 3))     # outside the clip: untouched
        self.assertEqual(cv.at(5, 5), (7, 7, 7))
        self.assertEqual(cv.at(9, 9), (7, 7, 7))
        self.assertEqual(cv.at(10, 10), (1, 2, 3))
        self.assertEqual(cv.at(9, 5), (9, 9, 9))
        cv.text(0, 6, "WWWWWW", (255, 255, 255), (4, 4, 4))   # a line of text keeps to the clip too
        cv.set_clip(0, 0, 20, 20)
        self.assertEqual(cv.at(4, 7), (1, 2, 3))
        self.assertEqual(cv.at(10, 7), (1, 2, 3))
        self.assertEqual(cv.at(5, 10), (1, 2, 3))
        self.assertIn(cv.at(6, 7), ((4, 4, 4), (255, 255, 255)))

    def test_text_is_drawn_with_the_glyphs_of_the_font_on_the_given_background(self):
        cv = sk.Canvas(40, 24)
        cv.text(1, 2, "I", sk.FG, sk.CARD_BG)
        px = {(x, y) for x in range(1, 9) for y in range(2, 10) if cv.at(x, y) == sk.FG}
        glyph = sk.GLYPHS["I"]
        want = {(1 + x, 2 + y) for y in range(8) for x in range(8) if glyph[y] >> x & 1}
        self.assertEqual(px, want)
        self.assertEqual(cv.at(2, 9), sk.CARD_BG)   # the glyph cell is painted with the background
        cv.text(1, 2, "I", sk.FG, sk.CARD_BG, 2)   # scale 2: each pixel is 2 x 2
        self.assertEqual(sum(1 for x in range(0, 20) for y in range(0, 20) if cv.at(x, y) == sk.FG), 4 * len(want))

    def test_every_ascii_character_has_a_glyph_and_other_text_is_folded(self):
        for code in range(0x20, 0x7f):
            self.assertIn(chr(code), sk.GLYPHS)
        for ch in "\u25b8\u22ef\u2026\u00b7\u203a\u00b6\u2713\u270e\u2315\u21c4\u25a4\u25c6\u03a3\u2192":
            self.assertIn(ch, sk.GLYPHS, hex(ord(ch)))   # the signs that the page puts in its cards
        self.assertEqual(sk.plain("caf\u00e9 \u201cquoted\u201d \u2014 x"), 'cafe "quoted" - x')
        self.assertEqual(sk.plain("a\nb"), "a\nb")
        self.assertEqual(sk.plain("\u4e2d"), "?")

    def test_the_picture_size_follows_the_window_and_the_pixel_budget(self):
        self.assertEqual(sk.geometry(100, 30, 900, 600)[:3], (900, 600, 1.0))
        w, h, shrink, unit = sk.geometry(200, 56, 2880, 1800)    # a Retina window
        self.assertEqual((w, h, shrink), (1440, 900, 2.0))
        self.assertLessEqual(w * h, 1300000)
        self.assertEqual(sk.geometry(80, 24, 0, 0)[:2], (720, 432))   # the terminal does not say its pixels: 9 x 18 a cell
        self.assertEqual(sk.geometry(50, 12, 1000, 800)[3], 4)   # big cells: bigger glyphs


class Program(AppCase):
    """The whole explorer on the demo store, with no terminal."""

    def test_it_starts_and_draws_the_map_of_the_seeded_store(self):
        app = self.make_app()
        self.assertEqual(len(app.roots), 9)
        self.assertEqual(len(app.edges), 2)
        self.assertEqual({n.session for n in app.roots}, {"tea", "tea-2", "bike", "bike-2", "garden", "parser", "trip", "bread", "today"})
        app.paint()
        self.assertGreaterEqual(len([b for b in app.boxes if b[5] == 0]), 9)
        w, h, raw = read_png(app.canvas.png())
        self.assertEqual((w, h), (app.W, app.H))
        colours = {raw[i:i + 3] for i in range(1, len(raw), 3 * 7 + 1)}
        self.assertGreater(len(colours), 5)   # boxes, text and chrome, not a blank picture

    def test_every_layout_draws_without_error_and_keeps_all_boxes(self):
        app = self.make_app()
        for key, _ in sk.LAYOUTS:
            app.set_layout(key)
            app.settle(seconds=40)
            self.assertEqual(app.layout, key)
            self.assertTrue(all(math.isfinite(v) for n in app.roots for v in (n.x, n.y, n.w, n.h)), key)
            self.assertGreaterEqual(len([b for b in app.boxes if b[5] == 0]), 4, key)
        app.set_layout("nonsense")
        self.assertEqual(app.layout, "grid")

    def test_the_trail_is_the_zoom_stack_with_the_rules_of_zoomed(self):
        app = self.make_app()
        a, b = app.roots[0], app.roots[1]
        app.focus(a.key)
        app.focus(b.key)
        self.assertEqual(app.stack, ["*", a.key, b.key])
        app.focus(b.key)                       # the top box again: back one step
        self.assertEqual(app.stack, ["*", a.key])
        app.focus(b.key)
        app.focus("*")                         # a box lower in the stack cuts the stack there
        self.assertEqual(app.stack, ["*"])
        for i in range(12):
            app.focus("k%d" % i)
        self.assertEqual(len(app.stack), 9)    # past 8 boxes the oldest leaves, with a gap mark
        self.assertEqual(app.dropped, 4)
        self.assertEqual(app.stack[0], "*")
        app.go_trail(2)
        self.assertEqual(len(app.stack), 3)
        app.home()
        self.assertEqual(app.stack, ["*"])
        app.back()
        self.assertEqual(app.stack, ["*"])

    def test_a_click_on_the_trail_goes_back_to_that_part(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        app.focus(today.key)
        app.settle()
        frame = today.doc.frames[1]
        app.focus(frame.key)
        app.settle()
        self.assertEqual(len(app.stack), 3)
        hit = next(h for h in app.hits if h[4] == "trail" and h[5] == 1)
        app.pixel_mouse = True
        app.handle(sk.Ev("mouse", action="press", x=(hit[0] + hit[2]) / 2, y=(hit[1] + hit[3]) / 2, button=0))
        self.assertEqual(app.stack, ["*", today.key])

    def test_a_session_opens_as_a_document_of_turns_bands_and_cards(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        app.focus(today.key)
        app.settle()
        d = today.doc
        self.assertEqual(d.state, "ready")
        self.assertEqual(len(d.frames), 5)                      # a frame for each user turn
        self.assertEqual([n.kind for n in d.bands], ["explore"])  # the one run of tools: crumb_find
        kinds = {n.kind for n in d.cards}
        self.assertLessEqual({"prompt", "narrative", "explore"}, kinds)
        self.assertEqual(sum(f.count for f in d.frames), len([n for n in d.cards]))
        self.assertTrue(all(math.isfinite(v) for n in d.all for v in (n.x, n.y, n.w, n.h)))

    def test_the_wheel_zooms_about_the_pointer_and_a_drag_pans(self):
        app = self.make_app()
        app.pixel_mouse = True
        k0, x0 = app.cam.k, app.cam.x
        wx = (300 - app.mid_x()) / app.cam.k + app.cam.x
        app.handle(sk.Ev("mouse", action="wheel", x=300, y=300, dy=1))
        self.assertTrue(app.manual)
        self.assertGreater(app.cam.tk, k0)
        self.assertEqual(app.cam.k, k0)   # the wheel moves only the target
        for _ in range(60):
            app.advance(1 / 30)
            self.assertAlmostEqual(app.sx(wx), 300.0, places=5)
        before = app.cam.x
        app.handle(sk.Ev("mouse", action="press", x=500, y=300, button=0))
        app.handle(sk.Ev("mouse", action="move", x=440, y=300, button=0))
        self.assertAlmostEqual(app.cam.x - before, 60 / app.cam.k)   # the pointer went 60 px left: the world went with it
        moved = app.cam.x
        app.handle(sk.Ev("mouse", action="move", x=400, y=300, button=0))
        self.assertAlmostEqual(app.cam.x - moved, 40 / app.cam.k)   # 1:1: the world moves with the pointer
        app.handle(sk.Ev("mouse", action="release", x=400, y=300, button=0))
        self.assertIsNone(app.drag)
        self.assertEqual(app.stack, ["*"])   # a drag is not a click

    def test_a_fast_drag_is_thrown_a_slow_one_is_not_and_motion_off_never_throws(self):
        for motion, fast, glides in ((True, True, True), (True, False, False), (False, True, False)):
            clock = Clock()
            app = self.make_app(motion=motion, clock=clock)
            app.pixel_mouse = True
            app.handle(sk.Ev("mouse", action="press", x=600, y=300, button=0))
            x = 600
            for _ in range(6):
                clock.tick(0.01 if fast else 0.2)
                x -= 30 if fast else 3
                app.handle(sk.Ev("mouse", action="move", x=x, y=300, button=0))
            if not fast:
                clock.tick(0.2)
            app.handle(sk.Ev("mouse", action="release", x=x, y=300, button=0))
            self.assertEqual(app.cam.gliding, glides, (motion, fast))
            if glides:
                self.assertGreater(app.cam.tx, app.cam.x)   # the hand went left, so the camera goes right
                app.handle(sk.Ev("mouse", action="press", x=100, y=300, button=0))   # a new press stops the glide
                self.assertFalse(app.cam.gliding)
                self.assertEqual((app.cam.tx, app.cam.ty), (app.cam.x, app.cam.y))

    def test_a_release_slower_than_120_px_a_second_does_not_glide_and_a_faster_one_than_4000_is_cut(self):
        self.assertEqual((sk.FLICK, sk.SPEED_MAX), (120.0, 4000.0))   # the numbers of the page (MOVE.flick, MOVE.fast)
        for speed, glides in ((100.0, False), (150.0, True), (3000.0, True), (9000.0, True)):
            clock = Clock()
            app = self.make_app(clock=clock)
            app.pixel_mouse = True
            x = 700.0
            app.handle(sk.Ev("mouse", action="press", x=x, y=300, button=0))
            for _ in range(10):
                clock.tick(0.01)
                x -= speed * 0.01
                app.handle(sk.Ev("mouse", action="move", x=x, y=300, button=0))
            app.handle(sk.Ev("mouse", action="release", x=x, y=300, button=0))
            self.assertEqual(app.cam.gliding, glides, speed)
            if glides:
                world_speed = min(speed, sk.SPEED_MAX) / app.cam.k    # the camera goes against the hand
                self.assertAlmostEqual((app.cam.tx - app.cam.x) * app.cam.k, min(speed, sk.SPEED_MAX) * sk.THROW_SECONDS, delta=0.5, msg=speed)
                self.assertAlmostEqual(app.cam.vx, world_speed, delta=0.01 * world_speed, msg=speed)

    def test_a_hand_that_stopped_before_it_let_go_gives_no_glide(self):
        clock = Clock()
        app = self.make_app(clock=clock)
        app.pixel_mouse = True
        app.handle(sk.Ev("mouse", action="press", x=700, y=300, button=0))
        for i in range(1, 9):
            clock.tick(0.01)
            app.handle(sk.Ev("mouse", action="move", x=700 - 30 * i, y=300, button=0))
        clock.tick(0.06)   # the hand rests for 60 ms, then the button comes up
        app.handle(sk.Ev("mouse", action="release", x=700 - 240, y=300, button=0))
        self.assertFalse(app.cam.gliding)

    def test_with_motion_off_the_wheel_is_a_jump(self):
        app = self.make_app(motion=False)
        app.pixel_mouse = True
        k0 = app.cam.k
        app.handle(sk.Ev("mouse", action="wheel", x=200, y=200, dy=2))
        self.assertAlmostEqual(app.cam.k, k0 * math.exp(2 * sk.WHEEL_STEP))

    def test_a_click_on_a_box_focuses_it_and_the_camera_eases_to_it(self):
        clock = Clock()
        app = self.make_app(clock=clock)
        app.pixel_mouse = True
        n = next(n for n in app.roots if n.session == "bike")
        b = next(b for b in app.boxes if b[0] is n)
        x, y = b[1] + b[3] / 2, b[2] + 4
        app.handle(sk.Ev("mouse", action="press", x=x, y=y, button=0))
        app.handle(sk.Ev("mouse", action="release", x=x, y=y, button=0))
        self.assertEqual(app.stack, ["*", n.key])
        self.assertFalse(app.manual)
        k = app.cam.k
        app.advance(1 / 60)
        app.paint()
        self.assertGreater(app.cam.k, k)
        self.assertLess(app.cam.k, 1.05 * k)   # the first frame barely moves: it starts slow
        for _ in range(240):
            app.advance(1 / 60)
        self.assertFalse(app.cam.moving())
        self.assertGreater(app.cam.k, 3 * k)

    def test_the_arrow_keys_move_the_focus_to_the_nearest_box_in_that_direction(self):
        for key, dx, dy in (("right", 1, 0), ("left", -1, 0), ("down", 0, 1), ("up", 0, -1)):
            app = self.make_app()
            app.paint()
            boxes = {b[0].key: b for b in app.boxes if b[5] == 0}
            app.handle(sk.Ev("key", key))
            self.assertEqual(len(app.stack), 2, key)
            b = boxes[app.stack[-1]]
            cx, cy = b[1] + b[3] / 2 - app.mid_x(), b[2] + b[4] / 2 - app.mid_y()
            self.assertGreater(cx * dx + cy * dy, 4, key)   # the box lies that way from the middle of the map
            near = [x for x in boxes.values() if (x[1] + x[3] / 2 - app.mid_x()) * dx + (x[2] + x[4] / 2 - app.mid_y()) * dy > 4]
            self.assertIn(b, near)

    def test_keys(self):
        app = self.make_app()
        app.handle(sk.Ev("char", text="2"))
        self.assertTrue(app.ctx_on)
        app.handle(sk.Ev("char", text="2"))
        self.assertFalse(app.ctx_on)
        app.handle(sk.Ev("char", text="4"))
        self.assertEqual(app.layout, "treemap")
        app.handle(sk.Ev("char", text="3"))
        self.assertTrue(app.follow_on)
        app.handle(sk.Ev("char", text="+"))
        self.assertTrue(app.manual)
        self.assertGreater(app.cam.tk, app.cam.k)
        app.handle(sk.Ev("char", text="1"))
        self.assertEqual(app.stack, ["*"])
        app.focus(app.roots[0].key)
        app.handle(sk.Ev("key", "enter"))   # the text of the box in the side panel
        self.assertTrue(app.side_on)
        app.handle(sk.Ev("key", "esc"))     # Escape closes the side panel first
        self.assertFalse(app.side_on)
        self.assertEqual(len(app.stack), 2)
        app.handle(sk.Ev("key", "esc"))     # then it goes back
        self.assertEqual(app.stack, ["*"])
        app.running = True
        app.handle(sk.Ev("char", text="q"))
        self.assertFalse(app.running)
        app.running = True
        app.handle(sk.Ev("key", "ctrl-c"))
        self.assertFalse(app.running)

    def test_find_in_memory_opens_the_first_hit_and_lights_the_sessions_that_hold_hits(self):
        app = self.make_app()
        app.handle(sk.Ev("char", text="/"))
        self.assertTrue(app.find_on)
        for ch in "kettle":
            app.handle(sk.Ev("char", text=ch))
        self.assertEqual(app.query, "kettle")
        app.handle(sk.Ev("key", "backspace"))
        app.handle(sk.Ev("char", text="e"))
        app.handle(sk.Ev("key", "enter"))
        app.settle()
        self.assertFalse(app.find_on)
        self.assertEqual(app.msg_text, "3 sessions hold hits; the first one is open")
        self.assertGreaterEqual(len(app.stack), 3)    # the session, and the card that holds the hit
        self.assertTrue(app.nodes[app.stack[-1]].glow > app.clock())
        self.assertGreaterEqual(len([n for n in app.roots if n.glow > app.clock()]), 3)
        app.handle(sk.Ev("char", text="/"))     # a find with no hit says so
        for ch in "zzzzqqqq":
            app.handle(sk.Ev("char", text=ch))
        app.handle(sk.Ev("key", "enter"))
        app.settle()
        self.assertEqual(app.msg_text, "no match")

    def test_escape_leaves_the_find_box_and_q_in_it_is_a_letter(self):
        app = self.make_app()
        app.running = True
        app.handle(sk.Ev("char", text="/"))
        app.handle(sk.Ev("char", text="q"))
        self.assertTrue(app.running)
        self.assertEqual((app.query, app.find_on), ("q", True))
        app.handle(sk.Ev("key", "esc"))
        self.assertFalse(app.find_on)
        self.assertTrue(app.running)

    def test_context_shows_the_gantt_and_the_venn_of_the_session_that_the_map_is_zoomed_to(self):
        app = self.make_app(size=(160, 45, 1600, 900))
        today = next(n for n in app.roots if n.session == "today")
        app.focus(today.key)
        app.settle()
        app.handle(sk.Ev("char", text="2"))
        app.settle()
        self.assertEqual(app.ctx_shown, "today")
        self.assertEqual([e["ev"] for e in app.ctx_data["events"]],
                         ["recall", "open", "recall", "find", "open", "compress", "recall", "open", "recall", "recall", "open", "open"])
        self.assertTrue(app.venn["regions"])
        app.paint()
        chips = [h for h in app.hits if h[4] == "chip"]
        bars = [h for h in app.hits if h[4] == "show"]
        self.assertEqual(len(chips), 12)
        self.assertGreater(len(bars), 12)
        app.do_action("chip", 4)       # a chip: the exact context that the event gave the model
        app.settle()
        self.assertEqual(app.ctx_at, 4)
        self.assertEqual(sorted(n for n, s in app.venn["sets"].items() if s["ids"]), ["found", "opened", "recalled"])
        self.assertTrue(app.side_on)
        self.assertEqual(app.side["kind"], "open")
        app.paint()
        app.do_action("side_close", None)
        self.assertFalse(app.side_on)

    def test_the_side_panel_shows_the_whole_text_of_a_crumb(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        app.show(today.id, 1.0, zoom=False)
        app.settle()
        self.assertTrue(app.side_on)
        self.assertEqual(app.RIGHT > 0, True)
        self.assertIn("My green tea is bitter again. What did we say about the kettle?", app.seen(today.id, 1.0))

    def test_the_agent_trail_lights_the_boxes_that_it_opened(self):
        app = self.make_app()
        app.follow_tick()
        self.assertTrue(app.agent_ids)
        self.assertTrue(app.agent_line.startswith("sess "))
        self.assertEqual(app.stack, ["*"])    # follow is off: the trail shows, the camera stays

    def test_follow_the_agent_zooms_the_map_to_the_box_that_the_agent_is_in(self):
        app = self.make_app()
        k0 = app.cam.tk
        app.follow_on = True
        app.follow_tick()
        app.settle(seconds=20)
        top = app.agent_ids[-1]
        self.assertEqual(app.nodes[app.stack[-1]].id, top)    # the map is zoomed to the box at the end of the agent's trail
        self.assertGreater(app.cam.tk, 2 * k0)
        stack = list(app.stack)
        app.follow_tick()                                      # the agent did not move: the map does not move either
        app.settle(seconds=5)
        self.assertEqual(app.stack, stack)

    def test_an_agent_that_stays_where_it_is_does_not_light_its_box_again_at_each_tick(self):
        app = self.make_app()
        app.follow_on = True
        app.follow_tick()
        app.settle(seconds=20)
        box = app.nodes[app.stack[-1]]
        self.assertGreater(box.glow, app.clock())           # the box lit when the agent came
        box.glow = 0.0
        app.follow_tick()                                    # the same place a second later
        app.settle(seconds=5)
        self.assertEqual(box.glow, 0.0)

    def test_show_in_the_map_finds_a_deep_crumb_through_the_path_of_its_session(self):
        app = self.make_app()
        today = next(n for n in app.roots if n.session == "today")
        app.focus(today.key)
        app.settle()
        card = next(n for n in today.doc.cards if n.kind == "prompt" and "bike" in n.head)
        app.focus("*")                                       # back to the overview: the crumb is found by its path
        app.settle()
        app.show(card.id, 1.0, zoom=True)
        app.settle(seconds=20)
        self.assertTrue(app.side_on)
        self.assertEqual(app.nodes[app.stack[-1]].id, card.id)
        self.assertIn(today.key, app.stack)

    def test_a_frame_is_sent_as_one_image_and_a_frame_that_did_not_change_is_not_sent_again(self):
        out = io.StringIO()
        app = self.make_app(out=out)
        self.assertTrue(app.present())
        first = out.getvalue()
        self.assertTrue(first.startswith("\x1b[H\x1b_Ga=T"))
        imgs = frames_in(first)
        self.assertEqual(len(imgs), 1)
        w, h, raw = read_png(imgs[0])
        self.assertEqual((w, h, raw), (app.W, app.H, bytes(app.canvas.buf)))   # what was sent is what was drawn
        self.assertIn(",c=100,r=30,", first)                                   # shown over the whole window
        for _ in range(5):
            self.assertFalse(app.present())     # nothing changed: not a byte more
        self.assertEqual(out.getvalue(), first)
        self.assertEqual((app.sent, app.skipped), (1, 5))
        app.say("something changed")
        self.assertTrue(app.present())
        self.assertEqual(len(frames_in(out.getvalue())), 2)

    def test_an_idle_viewer_is_idle(self):
        app = self.make_app()
        for _ in range(3):
            app.advance(1 / 30)
        self.assertFalse(app.animating())
        app.redraw = False
        app.advance(1 / 30)
        self.assertFalse(app.redraw)    # the loop would sleep: no frame to draw

    def test_a_resize_makes_a_new_picture_and_a_new_layout(self):
        app = self.make_app()
        app.resize(120, 40, 1200, 800)
        self.assertEqual((app.W, app.H), (1200, 800))
        self.assertTrue(app.dirty)
        app.settle()
        self.assertEqual(len(frames_in(self.present_text(app))), 1)

    def present_text(self, app):
        out = io.StringIO()
        app.out = out
        app.last_buf = None
        app.present()
        return out.getvalue()

    def test_a_far_zoom_in_and_a_far_zoom_out_draw_without_error(self):
        app = self.make_app()
        for steps in (60, -60, 100, -100):
            app.manual = True
            app.cam.zoom_at(app.mid_x(), app.mid_y(), steps, app.mid_x(), app.mid_y())
            app.cam.snap()
            app.paint()
            app.drain()
        self.assertTrue(math.isfinite(app.cam.k))

    def test_the_program_needs_a_terminal_and_says_so_without_a_traceback(self):
        app = sk.SpaceGraphKitty(store())
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                app.run()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("needs a terminal", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_folder_without_a_store_is_a_message_not_a_traceback(self):
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, contextlib.redirect_stderr(err):
            code = sk.main([empty, "--snapshot", os.path.join(empty, "x.png")])
        self.assertEqual(code, 1)
        self.assertIn("no store at", err.getvalue())

    def test_a_snapshot_is_one_settled_frame_as_a_png_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.png")
            self.assertEqual(sk.main([store(), "--snapshot", path, "--size", "640x360", "--layout", "treemap", "--context"]), 0)
            with open(path, "rb") as f:
                w, h, raw = read_png(f.read())
        self.assertEqual((w, h), (640, 360))
        self.assertGreater(len(set(raw[1::97])), 4)

    def test_the_command_line_passes_the_motion_flag_on(self):
        with mock.patch("breadcrumbs.spacegraph_kitty.SpaceGraphKitty") as klass:
            self.assertEqual(cli.main(["--home", store(), "spacegraph", "--motion", "0"]), 0)
            self.assertEqual(klass.call_args.kwargs["motion"], "0")
            cli.main(["--home", store(), "spacegraph"])
            self.assertIsNone(klass.call_args.kwargs["motion"])


class PageSemantics(unittest.TestCase):
    """The helpers that port the page's parsing of a session (view.html stepCalls, callDetail, toolOutcome)."""

    def test_a_step_is_its_narrative_then_one_line_for_each_call(self):
        text = 'Let me look.\nread_file {"path": "/a/b/c.py"}\nterminal {"command": "cd /x && ls -l"}\nmcp__tool_call {"calls": [{"name": "mcp__crumb", "arguments": {"id": "ab"}}]}'
        r = sk.step_calls(text)
        self.assertEqual(r["say"], "Let me look.")
        self.assertEqual([(c["nm"], sk.call_detail(c["nm"], c["args"])) for c in r["calls"]],
                         [("read_file", "c.py"), ("terminal", "ls -l"), ("crumb", "ab")])

    def test_a_call_whose_arguments_run_over_several_lines_is_joined(self):
        r = sk.step_calls('write_file {"path": "/x/y.py",\n "content": "a"}')
        self.assertEqual([(c["nm"], c["args"]["path"]) for c in r["calls"]], [("write_file", "/x/y.py")])

    def test_phases_files_and_outcomes(self):
        self.assertEqual(sk.PHASE_OF["read_file"], "explore")
        self.assertEqual(sk.PHASE_OF["patch"], "edit")
        self.assertEqual(sk.PHASE_OF["terminal"], "verify")
        self.assertEqual(sk.PHASE_OF["delegate_task"], "delegate")
        self.assertEqual(sk.call_targets("read_file", {"path": "/a/b"}), [("/a/b", "read")])
        self.assertEqual(sk.call_targets("patch", {"path": "/a/b"}), [("/a/b", "write")])
        self.assertEqual(sk.call_targets("terminal", {"path": "/a/b"}), [])
        self.assertEqual(sk.tool_outcome('{"success": false}'), "failed")
        self.assertEqual(sk.tool_outcome('{"exit_code": 2}'), "failed")
        self.assertEqual(sk.tool_outcome('{"exit_code": 0, "output": "x"}'), "ok")
        self.assertEqual(sk.tool_outcome("Traceback (most recent call last)"), "failed")
        self.assertEqual(sk.tool_outcome("plain text"), "")
        self.assertEqual(sk.tool_gist('{"output": "line one\\n\\nline two"}'), "line one\nline two")
        self.assertEqual(sk.call_detail("delegate_task", {"tasks": [1, 2, 3]}), "3 tasks")
        self.assertEqual(sk.call_detail("web_extract", {"urls": ["https://example.com/a/b?x=1"]}), "example.com/a/b")


if __name__ == "__main__":
    unittest.main()
