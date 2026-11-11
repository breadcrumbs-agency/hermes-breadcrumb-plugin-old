// LEVEL 10, `hive` (task F20). Hosts on the private swarm: a question goes out and is back inside its budget, or it passes.
//
// Data (nothing here is made up). D is bench/results/hive.json, key "b7ac1cc": the LoCoMo sweep after the two overload
// rules (the numbers of doc/HIVE.md). The Mac asks 1,981 questions; kali and win each answer from their own shard. That is
// 1,981 x 2 = 3,962 peer requests for each budget. D was copied by program; check-level-10.mjs reads it back and
// compares each value with the file.
//
// The picture. Each dot is one peer request. It is lit if the peer answered inside the budget; it takes the colour of its
// pass word (late, timeout, busy) if it passed. The count of dark dots is the sum of `passes` in the file: 3,281 at 5 ms,
// 15 at 10 ms, none at 150 ms. The file gives counts, not which request passed: the order of the dots is a fixed shuffle.
// The wall on the axis is the budget (log axis, ms). The two teal bars are the round trip of kali and win, p50 to p99,
// at 150 ms: the answers are a short mark at the left of the budget.
//
// lines = 1981 questions x 2 peers = 3962 = the dots in view.
(function (F) {
  'use strict';
  const { C, clamp, lerp, seg, rng, at, center } = F.kit;

  const D = /*hive.json b7ac1cc*/{"questions":1981,"k":10,"baseline_one_store":{"recall":0.6029,"ms_per_q":1.212},"budget_5_ms":{"recall":0.011,"passes":{"win":{"late":1441,"timeout":351,"busy":2},"kali":{"late":1045,"timeout":442}},"rt_p50":{"win":4.87,"kali":4.74},"rt_p99":{"win":5.44,"kali":5.36}},"budget_10_ms":{"recall":0.585,"passes":{"kali":{"timeout":2,"late":6},"win":{"timeout":2,"late":5}},"rt_p50":{"kali":5.65,"win":5.82},"rt_p99":{"kali":8.82,"win":8.44}},"budget_25_ms":{"recall":0.5887,"passes":{},"rt_p50":{"kali":5.76,"win":5.94},"rt_p99":{"kali":9.36,"win":9.3}},"budget_50_ms":{"recall":0.5887,"passes":{},"rt_p50":{"kali":5.92,"win":6.04},"rt_p99":{"kali":9.33,"win":8.97}},"budget_150_ms":{"recall":0.5887,"passes":{},"rt_p50":{"kali":5.48,"win":5.72},"rt_p99":{"kali":8.46,"win":8.15}}}/*end*/;

  const PEERS = ['kali', 'win'];                                   // the two shards; the Mac is the caller
  const NQ = D.questions;
  if (Object.keys(D.budget_150_ms.rt_p50).sort().join() !== [...PEERS].sort().join()) throw new Error('level 10: the peers of hive.json are not kali and win');
  const BUD = [5, 10, 150];                                        // the three questions: the budget in ms, as in the file
  const BK = BUD.map(b => `budget_${b}_ms`);
  const WORDS = ['late', 'timeout', 'busy'];                       // the pass words of these three budgets
  const dark = BK.map(k => PEERS.map(p => {                        // [late, timeout, busy] of each peer at each budget
    const o = D[k].passes[p] || {};
    if (Object.keys(o).some(w => !WORDS.includes(w))) throw new Error(`level 10: a pass word besides ${WORDS} in ${k}`);
    return WORDS.map(w => o[w] || 0);
  }));

  // ── the field: 53 x 38 dots for each peer (the last row holds 20) ─────────────
  const COLS = 53, ROWS = Math.ceil(NQ / COLS), PITCH = 0.015, HALF = 0.0042;
  const BX = 0.30, BY = { kali: -0.86, win: -0.17 };               // the top left dot of each block
  const dotX = i => BX + (i % COLS) * PITCH;
  const dotY = (h, i) => BY[PEERS[h]] + Math.floor(i / COLS) * PITCH;
  const midY = h => BY[PEERS[h]] + (ROWS - 1) * PITCH / 2;
  const rank = PEERS.map((p, h) => {                               // a fixed shuffle: which dots are the dark ones
    const r = rng(NQ + 7919 * h), idx = Array.from({ length: NQ }, (_, i) => i);
    for (let i = NQ - 1; i > 0; i--) { const j = Math.floor(r() * (i + 1)); [idx[i], idx[j]] = [idx[j], idx[i]]; }
    const rk = new Int32Array(NQ); idx.forEach((d, pos) => { rk[d] = pos; }); return rk;
  });

  // ── the places ─────────────────────────────────────────────────────────────
  const MAC = { x: -1.05, y: -0.25, r: 0.42 };
  const BELOW = { x: MAC.x, y: MAC.y, r: 0.24, phi: 0.35 };       // where the level below sits in the Mac: F31 may move it
  const AX = { x0: -0.95, x1: 1.25, y: 0.575, lo: 3, hi: 200 };    // the budget axis, in ms, log scale
  const ax = ms => AX.x0 + (AX.x1 - AX.x0) * Math.log(ms / AX.lo) / Math.log(AX.hi / AX.lo);
  const TICKS = [5, 10, 25, 50, 150];
  const SW = [1.5, 3.0, 4.1];                                      // u of the three questions (5 ms, 10 ms, 150 ms)
  const OUT = 0.22, BACK = [0.55, 0.8], SETTLE = [0.22, 0.62];     // seconds after a question: the pulse out, the pulse back, the dots settle
  const COL = [C.GREY, C.TEAL, C.ORANGE, C.RED, C.PURPLE];          // not asked, answered, late, timeout, busy
  const ALP = [0.28, 0.92, 0.9, 0.9, 0.95];
  const LEG = [[C.ORANGE, 'late'], [C.RED, 'timeout'], [C.PURPLE, 'busy']];

  function field(g, u) {
    const ctx = g.ctx, fade = seg(u, -0.2, 1.0), prog = SW.map(s => seg(u, s + SETTLE[0], s + SETTLE[1]));
    g.set();
    for (let h = 0; h < PEERS.length; h++) {
      const paths = COL.map(() => new Path2D()), rk = rank[h];
      for (let i = 0; i < NQ; i++) {
        const rf = Math.floor(i / COLS) / ROWS;                    // the sweep goes down the rows
        let s = -1; for (let q = BUD.length - 1; q >= 0; q--) if (rf < prog[q]) { s = q; break; }
        let cls = 0;
        if (s >= 0) { const [l, t, b] = dark[s][h], r = rk[i]; cls = r < l ? 2 : r < l + t ? 3 : r < l + t + b ? 4 : 1; }
        paths[cls].rect(dotX(i) - HALF, dotY(h, i) - HALF, 2 * HALF, 2 * HALF);
      }
      for (let c = 0; c < COL.length; c++) { ctx.globalAlpha = clamp(ALP[c] * fade * g.a); ctx.fillStyle = g.col(COL[c]); ctx.fill(paths[c]); }
    }
  }

  const me = F.def({
    name: 'hive', color: C.GOLD, R: 1.75, cx: -0.1, cy: 0,
    zoom: 2.2, dwell: 4.8, lines: NQ * PEERS.length,
    hint: ['150 ms: answer or pass', 1.2],
    place: () => at(BELOW.x, BELOW.y, BELOW.r, BELOW.phi, center(me.k - 1)),
    draw(g, { u }) {
      // the Mac: it asks. The level below lives in its ring (the engine draws it through place()).
      g.circle(MAC.x, MAC.y, MAC.r, { c: C.GOLD, w: 0.006, k: seg(u, -0.9, 0.1) });
      g.text('Mac', MAC.x, MAC.y + MAC.r + 0.07, { size: 0.034, font: 'mono', c: C.GOLD, a: seg(u, 0.1, 0.8) });
      // the swarm: one link to each shard, and the pulses of the questions
      const x1 = MAC.x + MAC.r, y1 = MAC.y;
      for (let h = 0; h < PEERS.length; h++) {
        const x2 = BX - 0.03, y2 = midY(h);
        g.line(x1, y1, x2, y2, { c: C.GREY, w: 0.004, a: 0.55, k: seg(u, -0.3, 0.7) });
        g.text(PEERS[h], BX, BY[PEERS[h]] - 0.05, { size: 0.034, font: 'mono', c: C.GOLD, align: 'left', a: seg(u, 0.1, 0.8) });
        for (const s of SW) {
          const o = seg(u, s, s + OUT), b = seg(u, s + BACK[0], s + BACK[1]);
          if (o > 0 && o < 1) g.dot(lerp(x1, x2, o), lerp(y1, y2, o), 0.012, C.WHITE);
          if (b > 0 && b < 1) g.dot(lerp(x2, x1, b), lerp(y2, y1, b), 0.012, C.TEAL);
        }
      }
      field(g, u);
      // the budget axis, the round trips (p50 to p99, 150 ms row) and the wall
      const a1 = seg(u, 0.0, 1.1), ta = seg(u, 0.6, 1.3);
      g.line(AX.x0, AX.y, AX.x1, AX.y, { c: C.GREY, w: 0.004, a: 0.8, k: a1 });
      for (const t of TICKS) {
        g.line(ax(t), AX.y - 0.012, ax(t), AX.y + 0.012, { c: C.GREY, w: 0.004, a: 0.8 * a1 });
        g.text(String(t), ax(t), AX.y + 0.065, { size: 0.03, font: 'mono', c: C.GREY, a: ta });
      }
      g.text('ms', AX.x1 + 0.09, AX.y, { size: 0.03, font: 'mono', c: C.GREY, a: ta });
      PEERS.forEach((p, h) => {
        const y = AX.y - 0.085 + 0.035 * h, k = seg(u, 0.8, 1.5);
        g.line(ax(D.budget_150_ms.rt_p50[p]), y, ax(D.budget_150_ms.rt_p99[p]), y, { c: C.TEAL, w: 0.012, a: 0.9, k });
      });
      const wx = lerp(lerp(ax(BUD[0]), ax(BUD[1]), seg(u, SW[1], SW[1] + 0.3)), ax(BUD[2]), seg(u, SW[2], SW[2] + 0.35));
      g.line(wx, AX.y - 0.15, wx, AX.y + 0.045, { c: C.YELLOW, w: 0.01, a: seg(u, SW[0] - 0.25, SW[0]) });
      // what the answers cost: the recall of the budget that has settled, and the pass words
      const done = SW.map(s => u >= s + SETTLE[1]), which = done.lastIndexOf(true);
      if (which >= 0) {
        const t0 = SW[which] + SETTLE[1], a = seg(u, t0, t0 + 0.15);
        g.text('recall', -1.45, AX.y - 0.075, { size: 0.03, font: 'mono', c: C.GREY, align: 'left', a: seg(u, SW[0] + 0.2, SW[0] + 0.6) });
        g.text(String(D[BK[which]].recall), -1.15, AX.y - 0.075, { size: 0.036, font: 'mono', c: C.TEAL, align: 'left', a });
        LEG.forEach(([c, w], i) => {
          const la = seg(u, SW[0] + 0.3, SW[0] + 0.7) * (1 - seg(u, SW[2] + SETTLE[1], SW[2] + SETTLE[1] + 0.4)), y = 0.33 + 0.045 * i;   // gone when no request passes
          g.dot(-1.43, y, 0.008, c, la);
          g.text(w, -1.40, y, { size: 0.028, font: 'mono', c: C.GREY, align: 'left', a: la });
        });
      }
    },
    tiny(g) {
      g.circle(MAC.x, MAC.y, MAC.r, { c: C.GOLD, w: 0.05, a: 0.9 });
      for (let h = 0; h < PEERS.length; h++) g.line(BX, midY(h), BX + 52 * PITCH, midY(h), { c: C.TEAL, w: 0.5, a: 0.7 });
    },
    // a strike for each question that goes out (one for each shard) and a drum when its dots settle
    ev: [].concat(...SW.map((s, q) => [
      [s, 'wood', 3 + 2 * q, 0.4, -0.35], [s + 0.03, 'wood', 5 + 2 * q, 0.34, 0.35],
      [s + SETTLE[1], 'drum', 2 * q, 0.5, 0],
    ])),
  });
})(Film);
