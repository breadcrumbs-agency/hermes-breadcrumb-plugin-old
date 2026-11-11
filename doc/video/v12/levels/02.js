// v12 level 2, `crumb` (task F12): one node, with its kind, title, ranges, kids and heritage. The caption is CR01 { k t r c h }.
// The node is the hero crumb e3b4789b. Its `r` field names the text that level 1 draws (spine fd3130cf, lines 0 to 157), so
// the level below sits in the `r` slot, and the corner count is those 157 lines (levels["2"].lines of store.json). The hero
// has no kids and no heritage: its blob leaves those two fields out. So the second node is the newest commit, 991408d6,
// which has the two fields that the hero has not. Its kid and its parent are drawn as ids only: the lines under them are
// not in view, so they are not in the count. One function prints both nodes, as five rows k t r c h; an empty row is a
// bare key letter and a short hairline, and it makes no sound.
// Colours: a document id (what `r` names) is TEAL, the colour of the level below in stubs/01.js; a crumb id is GOLD.
// Every string and number drawn here is in D, which is a copy of parts of store.json (F01). Nothing is made up.
// R, cx, cy = 1.7, -0.05, 0 hold the whole frame: the hero card, the ring and the commit card are all inside that disc.
(function (F) {
  'use strict';
  const { C, FONT, seg, pop, at, about, center, rrectPts } = F.kit;

  // DATA-BEGIN (parts of store.json; the done mark of F12 has the command that compares this block with the file)
  const D = {
    "source": "store.json sha256 2c5444e37dd98ca8c1c2e17257dcd6e7ceecf03027fa42ddc7662e6db7caae62",
    "name": "crumb",
    "lines": 157,
    "hero": {
      "id": "e3b4789b",
      "node": {
        "k": "tool",
        "t": "tool:read_file: 1|\"\"\"Immutable content-addressed blob store.",
        "r": [["fd3130cf", 0, 157]]
      }
    },
    "commit": {
      "id": "991408d6",
      "node": {
        "k": "commit",
        "t": "turn local-a1b9a02fff2e383a58c25d236ccce466342b19bc668f8e691",
        "c": ["d21f44ce"],
        "h": ["5e6119c8"]
      }
    }
  };
  // DATA-END

  const HERO = { node: D.hero.node, s: 0.044, x0: -1.55, y0: -0.08, a: 1, t0: 0.0 };
  const COMMIT = { node: D.commit.node, s: 0.03, x1: 1.42, y0: -0.77, a: 0.8, t0: 2.6 };
  const BELOW_R = 1.6;                          // R of the level below: levels/01.js has DISC = 1.6 (stubs/01.js has 1.6, too). If that R changes, change this one number.
  // the disc of the level below in this frame: centre and radius. Its y is the y of the `r` row of the hero (lay(): y0 + 1.6 s + 2.5 * 2.8 s),
  // so that the wire from the row to the ring is level.
  const SLOT = { x: 0.92, y: HERO.y0 + 8.6 * HERO.s, r: 0.415 };
  const PHI = 0.22;                             // the turn of the level below, which is the turn of the camera's spiral
  const RING = SLOT.r + 0.045;
  const TXT = '#c9ced6';
  const ROLE = { k: C.WHITE, t: TXT, doc: C.TEAL, kid: C.GOLD, n: TXT, p: C.GREY };

  // The width of a string in units of its text size, in the font that the page really has.
  const mctx = document.createElement('canvas').getContext('2d');
  const mw = s => { mctx.font = `100px ${FONT.mono}`; return mctx.measureText(s).width / 100; };

  // The runs of one field: [text, role]. k and t are words; r, c and h are lists, cut at each id and number.
  function toks(key, v) {
    if (key === 'k' || key === 't') return [[v, key]];
    const s = JSON.stringify(v), out = [];
    let last = 0;
    for (const m of s.matchAll(/[0-9a-f]{8}(?:\.\d+)?|\d+/g)) {
      if (m.index > last) out.push([s.slice(last, m.index), 'p']);
      out.push([m[0], /^[0-9a-f]{8}/.test(m[0]) ? (key === 'r' ? 'doc' : 'kid') : 'n']);
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push([s.slice(last), 'p']);
    return out;
  }

  // The layout of one card. S: node, s (text size), x0 or x1 (left or right edge), y0 (top), a (alpha), t0 (start, in u).
  function lay(S) {
    const s = S.s, keyW = 2.2 * s, padR = 2.4 * s, pitch = 2.8 * s, padY = 1.6 * s;
    const rows = ['k', 't', 'r', 'c', 'h'].map(key => ({ key, v: S.node[key] === undefined ? null : toks(key, S.node[key]) }));
    const vw = Math.max(...rows.map(r => r.v ? r.v.reduce((n, t) => n + mw(t[0]) * s, 0) : 0));
    const w = keyW + vw + padR, h = 5 * pitch + 2 * padY, x0 = S.x0 !== undefined ? S.x0 : S.x1 - w;
    rows.forEach((r, i) => {
      r.y = S.y0 + padY + (i + 0.5) * pitch;
      if (!r.v) return;
      let x = x0 + keyW;
      r.runs = r.v.map(([text, role]) => { const run = { text, role, x, w: mw(text) * s }; x += run.w; return run; });
    });
    return { x0, y0: S.y0, w, h, keyW, rows };
  }

  function card(g, S, u) {
    const L = lay(S), s = S.s, a = S.a, box = rrectPts(L.x0 + L.w / 2, L.y0 + L.h / 2, L.w / 2, L.h / 2, 0.035), kk = seg(u, S.t0, S.t0 + 0.9);
    // F30: the engine fills a polygon only when its k is 1, so fill + k in one call put the whole fill in at once (qa.py: a pop at
    // the end of each outline). The fill is its own polygon now and comes in with the outline.
    g.poly(box, { w: 0, closed: true, a, fill: C.GOLD, fa: 0.045 * kk });
    g.poly(box, { c: C.GOLD, w: 0.004, a, closed: true, k: kk });
    L.rows.forEach((r, i) => {
      const k = seg(u, S.t0 + 0.35 + 0.18 * i, S.t0 + 0.95 + 0.18 * i);
      g.text(r.key, L.x0 + s, r.y, { size: s, font: 'mono', c: r.v ? C.GOLD : C.GREY, a: (r.v ? 1 : 0.5) * a, align: 'left', k });
      if (r.v) for (const t of r.runs) g.text(t.text, t.x, r.y, { size: s, font: 'mono', c: ROLE[t.role], a: (t.role === 'p' ? 0.8 : 1) * a, align: 'left', k });
      else g.line(L.x0 + L.keyW, r.y, L.x0 + L.keyW + 3 * s, r.y, { c: C.GREY, w: 0.003, a: 0.45 * a, k });
    });
    return L;
  }

  // the link from the `r` row of the hero to the ring that holds the level below
  const wire = L => {
    const x0 = L.x0 + L.w, y0 = L.rows[2].y, x1 = SLOT.x - RING, y1 = SLOT.y, pts = [];
    for (let i = 0; i <= 24; i++) { const f = i / 24; pts.push([x0 + (x1 - x0) * f, y0 + (y1 - y0) * (f * f * (3 - 2 * f))]); }
    return pts;
  };
  const ARRIVE = 2.2;                           // the second at which the address reaches the ring and opens

  const me = F.def({
    name: D.name, color: C.GOLD, R: 1.7, cx: -0.05, cy: 0,
    zoom: 2.2, dwell: 4.8, lines: D.lines, view: { x: -0.05, y: 0 },
    hint: ['CR01 { k t r c h }', 1.7],
    place: () => at(SLOT.x, SLOT.y, SLOT.r / BELOW_R, PHI, center(me.k - 1)),
    draw(g, { u }) {
      const L = card(g, HERO, u);
      // the id: the checksum of the bytes that the card prints
      const q = pop(u, 1.5, 0.4);
      if (q > 0) { g.push(about(L.x0, L.y0 - 0.09, q)); g.text(D.hero.id, L.x0, L.y0 - 0.09, { size: 0.095, font: 'mono', c: C.GOLD, align: 'left' }); g.pop(); }
      // the range: the ring that holds the text, and the address that reaches it. The level below draws the id of that text in the ring.
      const lit = 0.55 + 0.45 * seg(u, ARRIVE, ARRIVE + 0.4);
      g.circle(SLOT.x, SLOT.y, RING, { c: C.TEAL, w: 0.004, a: lit, k: seg(u, -0.9, 0.3) });
      const P = wire(L), f = seg(u, 1.6, ARRIVE);
      g.poly(P, { c: C.TEAL, w: 0.004, a: 0.9, k: seg(u, 1.4, 1.9) });
      if (f > 0 && f < 1) { const p = P[Math.round(f * 24)]; g.dot(p[0], p[1], 0.013, C.WHITE, 0.95); }
      // the second node: the two fields that the hero has not
      const L2 = card(g, COMMIT, u);
      const q2 = seg(u, COMMIT.t0 + 0.2, COMMIT.t0 + 0.8);
      g.text(D.commit.id, L2.x0, L2.y0 - 0.045, { size: 0.045, font: 'mono', c: C.GOLD, a: 0.7, align: 'left', k: q2 });
    },
    // what stands for the level when it is small: the same silhouette (two cards, the wire, the ring), strokes wide enough to stay seen
    tiny(g) {
      const A = lay(HERO), B = lay(COMMIT);
      for (const L of [A, B]) g.poly(rrectPts(L.x0 + L.w / 2, L.y0 + L.h / 2, L.w / 2, L.h / 2, 0.035), { c: C.GOLD, w: 0.05, closed: true, a: 0.85, fill: C.GOLD, fa: 0.12 });
      g.line(A.x0 + A.w, A.rows[2].y, SLOT.x - RING, SLOT.y, { c: C.TEAL, w: 0.05, a: 0.8 });
      g.circle(SLOT.x, SLOT.y, RING, { c: C.TEAL, w: 0.05, a: 0.85 });
    },
    // a sound where something arrives, and none where a field is empty
    ev: [
      [0.50, 'pluck', 2, 0.22, -0.5], [0.68, 'pluck', 3, 0.22, -0.4], [0.86, 'pluck', 5, 0.22, -0.3],   // k, t, r of the hero are written
      [1.55, 'bell', 6, 0.30, -0.4],                                                                      // its id
      [ARRIVE, 'wood', 4, 0.40, 0.4],                                                                     // the address opens
      [3.20, 'pluck', 4, 0.18, 0.5], [3.38, 'pluck', 3, 0.18, 0.5],                                      // k, t of the commit
      [3.74, 'pluck', 6, 0.20, 0.5], [3.92, 'pluck', 7, 0.20, 0.5],                                      // its c and h
    ],
  });
})(Film);
