// Level 6, `rcpt` (F16): the chain of what the model saw. Drawn from store.json (the demo store at dump.store): the
// longest receipt chain, the 24 receipts of the session in levels["6"].session, oldest first, each holding in meta.prev
// the id of the receipt before it. The DATA line below is copied from store.json by levels/06.build.py, which also checks
// that 06.js is exactly that (--check); check-level-06.mjs checks the corner count; nothing else on the screen is typed in.
// What the picture shows:
//   - a tower for each receipt. Its number is text_lines, the lines of the text that the model saw; its height is the
//     square root of that number. Its colour is its kind (recall, find, open). The id is under it, the kind beside it.
//   - an arrow from each receipt to the one in its prev. The ref rcpt-<session> stands on the head.
//   - the two commits "turn <session>" of that session. Stored: kids, heritage (h: the older commit), ts, and in each
//     receipt meta.turn. A commit has no pointer to a receipt: the bracket groups the receipts with the same meta.turn,
//     and the commit is the one whose ts is after the last of them.
//   - the walk: the ref's head, then prev, prev, ... to the first receipt (prev ""), a wood block for each address that
//     opens. Then one strike on the first receipt: the text that the model saw, word for word.
// The corner count is `lines`: 10,622 = the sum of text_lines over the chain = store.json levels["6"].lines.
(function (F) {
  'use strict';
  const { C, lerp, seg, tnb, place, at, center, arrowHead, flash, ngon } = F.kit;
  // DATA (store.json: levels["6"], receipts[levels["6"].session].chain, commits of that session; first_text = chain[0].text)
  const D = {"lines":10622,"session":"local-4025dc8d1e81336d99274a7289ccf20aca11d6ff307d3a86881a80269385dca3","ref":"rcpt-local-4025dc8d1e81336d99274a7289ccf20aca11d6ff307d3a86881a80269385dca3","ref_short":"rcpt-local-4025dc8d1…","head":"32f69ee2","chain":[{"id":"f6e31f96","ev":"recall","lines":19,"prev":"","ts":1794352997,"turn":0,"ctx":"fd1dff82"},{"id":"006afcdb","ev":"find","lines":24,"prev":"f6e31f96","ts":1794353001,"turn":0,"ctx":"32ec3c7c"},{"id":"c3dd67d6","ev":"find","lines":22,"prev":"006afcdb","ts":1794353002,"turn":0,"ctx":"56fbba27"},{"id":"9fa84b83","ev":"find","lines":24,"prev":"c3dd67d6","ts":1794353002,"turn":0,"ctx":"c8dabb14"},{"id":"1d61eea4","ev":"find","lines":23,"prev":"9fa84b83","ts":1794353002,"turn":0,"ctx":"d3e45296"},{"id":"daf31d7c","ev":"find","lines":23,"prev":"1d61eea4","ts":1794353002,"turn":0,"ctx":"f515fbf7"},{"id":"4e0d9a64","ev":"find","lines":20,"prev":"daf31d7c","ts":1794353011,"turn":0,"ctx":"5c1a11b9"},{"id":"61f5237a","ev":"find","lines":18,"prev":"4e0d9a64","ts":1794353011,"turn":0,"ctx":"9a5e039b"},{"id":"c5c9a6b6","ev":"find","lines":19,"prev":"61f5237a","ts":1794353011,"turn":0,"ctx":"0d36fca5"},{"id":"2265fe92","ev":"find","lines":19,"prev":"c5c9a6b6","ts":1794353011,"turn":0,"ctx":"cb700716"},{"id":"260683c8","ev":"find","lines":19,"prev":"2265fe92","ts":1794353012,"turn":0,"ctx":"0b871d66"},{"id":"5d212191","ev":"recall","lines":13,"prev":"260683c8","ts":1794353127,"turn":1,"ctx":"7c4a7520"},{"id":"a0ed11f6","ev":"open","lines":1140,"prev":"5d212191","ts":1794353134,"turn":1,"ctx":"79a9a65b"},{"id":"ab961c72","ev":"open","lines":1630,"prev":"a0ed11f6","ts":1794353134,"turn":1,"ctx":"6e90cb63"},{"id":"a3120188","ev":"open","lines":2096,"prev":"ab961c72","ts":1794353134,"turn":1,"ctx":"fae81a70"},{"id":"af98e8c7","ev":"open","lines":2029,"prev":"a3120188","ts":1794353135,"turn":1,"ctx":"7547546d"},{"id":"adb9cfc0","ev":"open","lines":3324,"prev":"af98e8c7","ts":1794353135,"turn":1,"ctx":"9d07a9c3"},{"id":"3a3bd77d","ev":"open","lines":30,"prev":"adb9cfc0","ts":1794353142,"turn":1,"ctx":"9cad124e"},{"id":"f7daa76c","ev":"open","lines":19,"prev":"3a3bd77d","ts":1794353142,"turn":1,"ctx":"ddb806f5"},{"id":"8c44fc35","ev":"open","lines":19,"prev":"f7daa76c","ts":1794353143,"turn":1,"ctx":"74a1b2a5"},{"id":"9ef97a73","ev":"open","lines":26,"prev":"8c44fc35","ts":1794353143,"turn":1,"ctx":"40727f59"},{"id":"11c74a49","ev":"open","lines":25,"prev":"9ef97a73","ts":1794353143,"turn":1,"ctx":"da385144"},{"id":"7eeb0497","ev":"open","lines":26,"prev":"11c74a49","ts":1794353149,"turn":1,"ctx":"4a12cca3"},{"id":"32f69ee2","ev":"open","lines":15,"prev":"7eeb0497","ts":1794353149,"turn":1,"ctx":"914b19fb"}],"commits":[{"id":"1c99db4b","t":"turn local-4025dc8d1e81336d99274a7289ccf20aca11d6ff307d3a868","ts":1794353101,"h":["73932815"]},{"id":"5e6119c8","t":"turn local-4025dc8d1e81336d99274a7289ccf20aca11d6ff307d3a868","ts":1794353161,"h":["1c99db4b"]}],"first_text":"Breadcrumbs from earlier sessions (expand with crumb(id, level)):\n▸ 27319047 tool ·2254t\n213 … IPFS … kubo … pull … hive\n  ⋯ 289f5e15:1-116\n▸ f9392b44 tool ·2412t\nmemory provider … external\n  ⋯ e88c8454:1-159\n▸ 332e4006 tool ·2412t\nmemory provider … external\n  ⋯ e88c8454:1-159\n▸ 2372b988 tool ·3462t\nwindow ACE\n  ⋯ e42ecc7f:1-161\n▸ 74ac1d01 tool ·3462t\nwindow ACE\n  ⋯ e42ecc7f:1-161\n▸ 76331fc3 tool ·2038t\npython … DIR\n  ⋯ b2214aae:1-157\n"};
  // END DATA

  // ── the data must be what the picture claims ────────────────────────────────────────────────────
  const N = D.chain.length, IDX = new Map(D.chain.map((r, i) => [r.id, i]));
  if (N < 2) throw new Error('level 6: the chain has fewer than 2 receipts');
  if (D.chain.reduce((a, r) => a + r.lines, 0) !== D.lines) throw new Error('level 6: the lines of the chain are not the lines of the dump');
  D.chain.forEach((r, i) => { if (r.prev !== (i ? D.chain[i - 1].id : '')) throw new Error(`level 6: receipt ${r.id}: prev is not the receipt before it`); });
  if (D.chain[N - 1].id !== D.head) throw new Error('level 6: the newest receipt is not the head of the ref');
  if (D.commits.length !== 2 || D.commits[1].h[0] !== D.commits[0].id) throw new Error('level 6: the newer commit does not point to the older');
  if (D.first_text.split('\n').length < D.chain[0].lines) throw new Error('level 6: the text of the first receipt is shorter than its lines');

  // ── geometry (world units; at the full view the screen is 3.56 x 2 about the view) ──────────────
  const PITCH = 0.12, HW = 0.034, CX = -0.16, Y0 = 0.22, YA = Y0 - 0.014, LS = 0.026;
  const YC = 0.50, YH = 0.60;                                 // the turn brackets, and the line of the heritage pointer
  const MAXL = Math.max(...D.chain.map(r => r.lines));
  const xOf = i => CX + (i - (N - 1) / 2) * PITCH;
  const hOf = r => 0.06 + 0.84 * Math.sqrt(r.lines / MAXL);
  const EVC = { recall: C.YELLOW, find: C.BLUE, open: C.GREEN };
  const fmt = n => n.toLocaleString('en-US');
  const WIN = [0.80, -0.45];                                  // where level 5 (the window, R 1.75) stands, at 0.09 of its size
  const PX = -1.60, PTOP = -0.80, PLH = 0.036, PTS = 0.028, PW = 1.14;   // the strike panel
  const FIRST = D.first_text.split('\n').slice(0, D.chain[0].lines);
  const PBOT = PTOP + 0.085 + FIRST.length * PLH;

  // The turns: the receipts with the same meta.turn, and the commit whose ts follows the last of them.
  const TURNS = D.commits.map((c, k) => {
    const idx = D.chain.map((r, i) => i).filter(i => D.chain[i].turn === k);
    const first = idx[0], after = idx[idx.length - 1];
    if (!idx.length || D.chain[after].ts >= c.ts || (after + 1 < N && D.chain[after + 1].ts < c.ts)) throw new Error(`level 6: commit ${c.id} does not follow turn ${k}`);
    return { c, first, after, x: after < N - 1 ? (xOf(after) + xOf(after + 1)) / 2 : xOf(after) + 0.1 };
  });

  // ── the timeline, in seconds since the level's origin (the full view holds from u = zoom / 2 = 1.3 to 5.7) ──
  const growT = i => 0.05 + 0.028 * (N - 1 - i);                // the towers rise from the head back
  const W0 = 2.2, WS = 0.06;                                  // the walk: the head first, one receipt for each 0.06 s
  const walkT = i => W0 + WS * (N - 1 - i);
  const S0 = W0 + WS * N + 0.16;                              // the strike on the first receipt
  const lit = (i, u) => seg(u, walkT(i), walkT(i) + 0.12);
  const pulse = (i, u) => tnb((u - walkT(i)) / 0.3);
  const SKY = [[xOf(0) - HW, Y0]];
  D.chain.forEach((r, i) => SKY.push([xOf(i) - HW, Y0 - hOf(r)], [xOf(i) + HW, Y0 - hOf(r)]));
  SKY.push([xOf(N - 1) + HW, Y0]);

  // a falling run of wood blocks down the chain, then the strike on the first receipt, at the pitch of the lowest step
  const ev = [];
  for (let j = 0; j < N; j++) ev.push([W0 + WS * j, 'wood', 8 - Math.floor(j * 8 / N), 0.2, lerp(0.6, -0.6, j / (N - 1))]);
  ev.push([S0, 'wood', 0, 0.5, -0.6]);

  const me = F.def({
    name: 'rcpt', color: C.ORANGE, R: 1.7, cx: -0.08, cy: -0.02,
    zoom: 2.6, dwell: 4.4, lines: D.lines, view: { x: -0.08, y: -0.02 },
    hint: ['rcpt → rcpt → commit', 1.5],
    place: () => at(WIN[0], WIN[1], 0.09, 0.1, center(me.k - 1)),
    draw(g, { u }) {
      g.line(xOf(0) - 0.07, Y0, xOf(N - 1) + 0.07, Y0, { c: C.GREY, w: 0.003, a: 0.5 * seg(u, 0, 0.4) });
      for (let i = 0; i < N; i++) {
        const r = D.chain[i], x = xOf(i), grow = seg(u, growT(i), growT(i) + 0.45);
        if (grow <= 0) continue;
        const lt = lit(i, u), pu = pulse(i, u), col = EVC[r.ev], top = Y0 - hOf(r) * grow;
        g.poly([[x - HW, Y0], [x - HW, top], [x + HW, top], [x + HW, Y0]],
          { c: lt > 0.5 ? C.WHITE : col, w: 0.004 + 0.004 * pu, a: 0.55 + 0.45 * lt, closed: true, fill: col, fa: 0.12 + 0.12 * lt + 0.25 * pu });
        g.text(fmt(r.lines), x, top - 0.03, { size: LS, font: 'mono', c: C.WHITE, a: grow * (0.6 + 0.4 * lt) });
        g.push(place(x - 0.02, Y0 + 0.035, 1, Math.PI / 2));
        g.text(r.id, 0, 0, { size: LS, font: 'mono', align: 'left', c: C.WHITE, a: grow * (0.5 + 0.5 * lt) });
        g.pop();
        g.push(place(x + 0.02, Y0 + 0.035, 1, Math.PI / 2));
        g.text(r.ev, 0, 0, { size: LS, font: 'mono', align: 'left', c: col, a: grow * (0.55 + 0.4 * lt) });
        g.pop();
        if (i) {   // the arrow from this receipt to the one in its prev
          const j = IDX.get(r.prev), xa = x - HW - 0.004, xb = xOf(j) + HW + 0.004;
          const al = seg(u, growT(i) + 0.25, growT(i) + 0.6), lk = seg(u, walkT(i) + 0.03, walkT(i) + 0.13);
          g.line(xa, YA, xb, YA, { c: C.GREY, w: 0.004, a: 0.55 * al });
          arrowHead(g, xb, YA, Math.PI, 0.016, C.GREY, 0.55 * al);
          if (lk > 0) { g.line(xa, YA, xb, YA, { c: C.WHITE, w: 0.005, a: lk }); arrowHead(g, xb, YA, Math.PI, 0.017, C.WHITE, lk); }
        }
      }
      // the ref stands on the head
      const hd = D.chain[N - 1], kRef = seg(u, 1.4, 2.2), refTop = Y0 - hOf(hd) - 0.07;
      if (kRef > 0) {
        g.line(xOf(N - 1), Y0 - hOf(hd) - 0.045, xOf(N - 1), refTop - 0.01, { c: C.ORANGE, w: 0.004, a: 0.9 * kRef });
        g.push(place(xOf(N - 1), refTop - 0.02, 1, -Math.PI / 2));
        g.text(D.ref_short, 0, 0, { size: LS, font: 'mono', align: 'left', c: C.ORANGE, a: 0.95, k: kRef });
        g.pop();
      }
      // the turns and their commits
      TURNS.forEach((tr, k) => {
        const kk = seg(u, 1.1 + 0.2 * k, 1.7 + 0.2 * k);
        if (kk <= 0) return;
        const x0 = xOf(tr.first) - HW, x1 = tr.x - 0.03;
        g.line(x0, YC, x1, YC, { c: C.ORANGE, w: 0.004, a: 0.8, k: kk });
        for (let i = tr.first; i <= tr.after; i++) g.line(xOf(i), YC - 0.03, xOf(i), YC, { c: C.ORANGE, w: 0.003, a: 0.6 * kk });
        g.poly(ngon(4, 0.03, -Math.PI / 2, tr.x, YC), { c: C.ORANGE, w: 0.005, a: kk, closed: true, fill: C.ORANGE, fa: 0.3 });
        g.text(tr.c.id, tr.x, YH + 0.05, { size: LS, font: 'mono', c: C.ORANGE, a: kk });
        g.text(tr.c.t, tr.x + (k ? 0.0625 : 0), YH + 0.105, { size: 0.024, font: 'mono', align: k ? 'right' : 'center', c: C.GREY, a: kk });
      });
      const kh = seg(u, 1.7, 2.0);   // the newer commit points to the older one (h)
      if (kh > 0) {
        const a = TURNS[1], b = TURNS[0];
        g.poly([[a.x, YC + 0.03], [a.x, YH], [b.x, YH], [b.x, YC + 0.045]], { c: C.ORANGE, w: 0.004, a: 0.8, k: kh });
        arrowHead(g, b.x, YC + 0.045, -Math.PI / 2, 0.016, C.ORANGE, kh);
      }
      // the strike: the first receipt opens to the text that the model saw, whole
      const ks = seg(u, S0, S0 + 0.35);
      if (ks > 0) {
        const r0 = D.chain[0], x = xOf(0), top0 = Y0 - hOf(r0);
        flash(g, x, top0, 0.08, (u - S0) / 0.4, C.WHITE);
        g.poly([[PX - 0.04, PTOP], [PX + PW + 0.04, PTOP], [PX + PW + 0.04, PBOT], [PX - 0.04, PBOT]], { c: C.WHITE, w: 0.004, a: 0.7, closed: true, k: ks });
        g.line(x, PBOT, x, top0 - 0.06, { c: C.WHITE, w: 0.004, a: 0.7 * ks });
        g.text(r0.id, PX, PTOP + 0.045, { size: PTS, font: 'mono', align: 'left', c: C.WHITE, k: ks });
        g.text(r0.ev, PX + 0.30, PTOP + 0.045, { size: PTS, font: 'mono', align: 'left', c: EVC[r0.ev], k: ks });
        g.text(r0.ctx, PX + PW, PTOP + 0.045, { size: PTS, font: 'mono', align: 'right', c: C.ORANGE, k: ks });
        FIRST.forEach((ln, j) => {
          const a = S0 + 0.15 + 0.025 * j;
          if (ln) g.text(ln, PX, PTOP + 0.085 + (j + 0.5) * PLH, { size: PTS, font: 'mono', align: 'left', c: C.WHITE, a: 0.92, k: seg(u, a, a + 0.25) });
        });
      }
    },
    tiny(g) {
      g.poly(SKY, { c: C.ORANGE, w: 0.05, a: 0.8, closed: true, fill: C.ORANGE, fa: 0.25 });
    },
    ev,
  });
})(Film);
