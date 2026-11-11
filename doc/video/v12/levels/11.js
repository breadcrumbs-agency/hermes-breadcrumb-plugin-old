// levels/11.js: level 11, `ouroboros` (task F21), the last level of the film (close: true).
// Drawn from store.json (task F01), the demo store. Its main chain of 29 commits holds 18 chats (by their titles in
// map.graph.nodes: 13 "Read <file>", 2 load tests, 1 hard check of the code, 2 probes), all with breadcrumbs as their
// memory. The ring is those 18 chats, the oldest at the top,
// one hexagon each (size: log10 of the lines of its text), named by its root id: the address that crumb(id, level)
// takes. Yellow: the dogfood chat of levels 4 and 5. A dashed arc from the newest to the oldest closes the ring.
// The chords are the cue edges of the explorer's map (map.graph.edges): the first word that two chats share.
// The words fall away; the addresses stay. Then the ring and the whole climb under it (levels 10 to 0, nested by
// place()) fall in on the middle, and level 0's own frame at u = 0 comes back (kit.drawLevel(0, ...)): the last frame of
// the film is its first. That holds while V0 below is the view of level 0 and this level keeps the default view {x: 0, y: 0, v: 1}.
// The corner is LINES: store.json levels.11.lines = counts.line_positions, every text of the store.
// Time (u, seconds from the origin; the flight in ends at u = 1.3): 0.5 to 1.8 the blocks open, one wood block each;
// 2.0 the newest reaches the oldest (drum, flash); 2.05 to 2.9 the chords; 2.9 the words fall; 3.3 to 4.8 the ring and
// the climb fall in; 4.35 to 4.95 level 0's first frame returns; 5.7 the end.
// Sound: 18 wood blocks (one for each address, rising along the pentatonic, panned to where the block sits), a flute
// line of three notes, one drum at 2.0. Nothing sounds after u = 2.0, 3.7 s before the end: the reverb of score.py is
// circular, and a drum that close to the end leaves no measurable tail at the opening of the loop (measured by taking
// the event out: 0.0002 at the end of the file, 0.0000 in its first 3 s). A bell there did (0.087 and 0.027): it is out.
(function (F) {
  'use strict';
  const { C, TAU, clamp, lerp, sm, seg, pop, place, at, ngon, flash, center, drawLevel } = F.kit;
  // DATA-BEGIN (store.json: levels.11 {ref, lines}; hero.session_root; map.sessions in the order of the first commit of each chat, with lines; map.graph.edges as [source, target, first shared word])
  const DATA = {"ref":"991408d6","lines":38534,"hero":"3f77b3b8","chats":[["cd5bbeab",15],["c1ee903b",30],["1f095264",31],["a56a1fc8",50],["d1840675",74],["c7725ddc",70],["66dc64f2",88],["b47ed5dd",70],["21c8a6a7",40],["03562ec8",60],["ac2889c6",50],["07860568",15],["3f77b3b8",8448],["4d434b6c",8323],["c2d009cd",946],["8444703a",78],["de23b06f",2044],["d21f44ce",2]],"chords":[["4d434b6c","3f77b3b8","const"],["07860568","cd5bbeab","plugin"],["1f095264","cd5bbeab","license"],["d21f44ce","c7725ddc","level"],["8444703a","3f77b3b8","bcdemo"],["c2d009cd","c7725ddc","lines"],["ac2889c6","a56a1fc8","memory"],["21c8a6a7","66dc64f2","demo"],["1f095264","c1ee903b","license"],["c1ee903b","cd5bbeab","license"]]};
  // DATA-END
  const { chats: CHATS, chords: CHORDS, ref: REF, hero: HERO, lines: LINES } = DATA;

  const N = CHATS.length, RING = 0.72, TH0 = -Math.PI / 2, STEP = TAU / N;
  const th = i => TH0 + STEP * i;
  const sz = i => 0.02 + 0.0105 * Math.log10(CHATS[i][1]);
  const UB = i => 0.5 + 0.075 * i;                 // when block i opens
  const G1 = 2.0;                                  // when the newest reaches the oldest
  const FALL = 2.9;                                // when the words let go
  const COL = [3.3, 4.8];                          // the ring and the climb fall in on the middle
  const FIRST = [4.35, 4.95];                      // level 0's frame at u = 0 comes back
  const NEST = 0.26, TILT = -0.3;                  // where the climb sits in the ring
  // The view of level 0 (its `view` in levels/00.js: {x: 0, y: VY = 0.13, v: VV = 0.67} at 18:33). The camera shows level 0 at this
  // view in the first frame, so over() puts level 0's frame where this level's own view (the default) shows it the same.
  // If levels/00.js sets another view, set it here too, or the last frame is not the first (check-level-11.mjs and F31 check that they are equal).
  const V0 = { x: 0, y: 0.13, v: 0.67 };
  const MY_VIEW = { x: 0, y: 0, v: 1 };            // the view of this level (the engine's default, said out loud)
  const SAME = (s => place(MY_VIEW.x - s * V0.x, MY_VIEW.y - s * V0.y, s, 0))(MY_VIEW.v / V0.v);
  const idx = id => { const i = CHATS.findIndex(c => c[0] === id); if (i < 0) throw new Error('level 11: no chat ' + id); return i; };
  const CH = CHORDS.map(([a, b, w]) => ({ a: idx(a), b: idx(b), w }));
  let ORIGIN = 0;

  // the ring at second u: radius r and the turn of its fall
  const geo = u => { const q = seg(u, COL[0], COL[1]); return { q, r: RING * (1 - q), spin: 1.4 * q }; };
  const at_ = (i, G) => [G.r * Math.cos(th(i) + G.spin), G.r * Math.sin(th(i) + G.spin)];
  // the arc of the ring from block i0 to block i1 (a block is STEP radians from the next), n segments
  const arc = (i0, i1, G, n) => Array.from({ length: n + 1 }, (_, j) => { const a = TH0 + STEP * lerp(i0, i1, j / n) + G.spin; return [G.r * Math.cos(a), G.r * Math.sin(a)]; });

  const me = F.def({
    name: 'ouroboros', color: C.GREEN, R: 1.05, cx: 0, cy: 0, zoom: 2.6, dwell: 4.4, lines: LINES, close: true, view: MY_VIEW,
    hint: ['agents share evidence, not conversations', 1.1],
    init(o) { ORIGIN = o; },
    // the climb sits in the middle of the ring; after the flight in it falls in on the middle with the ring
    place: T => { const q = seg(T - ORIGIN, COL[0], COL[1]); return at(0, 0, lerp(NEST, 0.003, q), TILT + 1.1 * q, center(me.k - 1)); },
    heroAlpha: u => 1 - seg(u, COL[0] + 0.5, COL[1] - 0.2),
    draw(g, { u }) {
      const G = geo(u), live = 1 - seg(u, COL[0] + 1.0, COL[1]);
      if (live <= 0) return;
      // the chain, oldest to newest, grows round the ring; the dashed arc from the newest to the oldest closes it
      const kArc = clamp((u - UB(0)) / (UB(N - 1) - UB(0)), 0, 1);   // linear: the head of the arc meets each block as it opens
      if (kArc > 0) g.poly(arc(0, N - 1, G, 170), { c: C.GREEN, w: 0.008, a: 0.5 * live, k: kArc });
      const kc = seg(u, G1 - 0.3, G1);
      if (kc > 0) g.poly(arc(N - 1, N, G, 10), { c: C.WHITE, w: 0.006, a: 0.8 * live, k: kc, dash: [0.02, 0.015] });
      // the evidence: the cue edges of the map
      CH.forEach((c, j) => {
        const k = seg(u, 2.05 + 0.04 * j, 2.55 + 0.04 * j);
        if (k <= 0) return;
        const P = at_(c.a, G), Q = at_(c.b, G);
        g.line(P[0], P[1], Q[0], Q[1], { c: C.TEAL, w: 0.004, a: 0.32 * live, k });
      });
      // the blocks and their addresses
      for (let i = 0; i < N; i++) {
        const o = pop(u, UB(i), 0.4);
        if (o <= 0) continue;
        const s = sz(i) * o * (1 - 0.9 * G.q), [x, y] = at_(i, G), hero = CHATS[i][0] === HERO;
        g.poly(ngon(6, s, th(i) + G.spin, x, y), { c: hero ? C.YELLOW : i % 2 ? C.TEAL : C.GREEN, w: s * 0.12, closed: true, fill: '#0e1f12', fa: 0.92, a: live });
        const la = 1 - seg(u, 3.3, 3.9);
        if (la > 0) {
          const cs = Math.cos(th(i)), sn = Math.sin(th(i)), RL = RING + sz(i) + 0.04;
          const al = cs > 0.25 ? 'left' : cs < -0.25 ? 'right' : 'center';
          g.text(CHATS[i][0], RL * cs, RL * sn + (al === 'center' ? (sn > 0 ? 0.035 : -0.035) : 0), { font: 'mono', size: 0.03, c: hero ? C.YELLOW : C.GREY, a: la, align: al, k: seg(u, UB(i) + 0.1, UB(i) + 0.5) });
        }
      }
      // the address of the whole: the head of the chain, the ref of this level
      const lr = 1 - seg(u, 3.3, 3.9);
      if (lr > 0) g.text(REF, 0, 0.58, { font: 'mono', size: 0.032, c: C.WHITE, a: lr, k: seg(u, G1 + 0.1, G1 + 0.7) });
      // the incidental words: they appear on the chords, then they fall (a word that two or more chords share is written once)
      CH.forEach((c, j) => {
        const k = seg(u, 2.3 + 0.05 * j, 2.7 + 0.05 * j), wa = k * (1 - seg(u, FALL, FALL + 0.6));
        if (wa <= 0 || CH.findIndex(o => o.w === c.w) !== j) return;
        const G0 = geo(0), P = at_(c.a, G0), Q = at_(c.b, G0), L = Math.hypot(Q[0] - P[0], Q[1] - P[1]), s = L > 0.7 ? 0.2 : 0.5;
        let wx = lerp(P[0], Q[0], s), wy = lerp(P[1], Q[1], s);
        if (L <= 0.7) { const d = Math.hypot(wx, wy) || 1; wx -= wx / d * 0.07; wy -= wy / d * 0.07; }
        const dt = Math.max(0, u - FALL);
        g.text(c.w, wx, wy + 1.2 * dt * dt, { font: 'mono', size: 0.026, c: C.GREY, a: wa, k: Math.min(1, k * 1.2) });
      });
      // the bite
      const jx = RING * Math.cos(TH0 + STEP * (N - 0.5)), jy = RING * Math.sin(TH0 + STEP * (N - 0.5));
      flash(g, jx, jy, 0.14, (u - G1) / 0.6, C.WHITE, 14);
      if (u > G1 && u < G1 + 0.9) g.circle(0, 0, lerp(RING, 1.5, sm((u - G1) / 0.9)), { c: C.WHITE, w: 0.005, a: 0.6 * (1 - (u - G1) / 0.9) * clamp((u - G1) / 0.07) });   // in over four frames: a hit, not a one-frame pop
    },
    over(g, { u, hero }) {
      // level 0's own frame at u = 0, painted again over the middle: the first frame of the film
      const a = hero ? seg(u, FIRST[0], FIRST[1]) : 0;
      if (a <= 0) return;
      g.push(SAME, a);
      drawLevel(0, g, { u: 0, T: 0, hero: true });
      g.pop();
    },
    tiny(g) { g.circle(0, 0, RING, { c: C.GREEN, w: 0.06, a: 0.85 }); g.dot(0, -RING, 0.1, C.YELLOW, 0.9); },
    ev: [
      // a wood block for each address that opens, rising along the pentatonic, panned to where the block is
      ...CHATS.map((_, i) => [UB(i), 'wood', 2 + Math.floor(i * 10 / (N - 1)), 0.4, 0.6 * Math.cos(th(i))]),
      [0.5, 'flute', 4, 0.35, -0.2], [0.95, 'flute', 6, 0.35, 0], [1.4, 'flute', 8, 0.35, 0.2],
      [G1, 'drum', 0, 0.5, 0],
    ],
  });
})(Film);
