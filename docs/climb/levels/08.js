// v12 level 8, `family` (task F18). A lead and its five children: each child's session left as a CAR file with a CID,
// and the lead imported it under the same root id. Drawn from the kept run of H01 (hive/hive-run.json, hive/run/numbers.json).
//
// The data block below is a copy of what this level draws, made by a command from those files, the five CAR files
// (read with share.read_car of the code of the H01 run) and store.json; its src hashes name the files it was made from. Nothing else
// in this file is data. The count in the corner (`lines`) is level 7's lines (store.json levels.7.lines: the lead's
// memory, the level below) plus the line positions under the five roots in the CARs (a crumb's ranges, then its kids,
// recursively: the definition of dump.py); it is 31,645.
//   child k      its area (the brief), its root id, its CAR (cid, blocks, car_bytes); `crumbAt` is the place in the file of
//                each crumb block (the other blocks are lines); `dur` is the seconds the child lived (numbers.json)
//   the CARs land in the order the children really finished, each after a time that grows with the seconds its child lived
//   (`dur`); the gauge draws totals and lead_window of hive-run.json, each number under its own name; check-level-08.py
//   checks this block against the files
(function (F) {
  'use strict';
  const { C, TAU, clamp, lerp, sm, seg, at, ap, center, rrectPts, flash } = F.kit;
  const D = /*DATA*/{"src":{"hive_run_sha256":"f93e8920ea7f3b31971fdb5435c69c10513369c6689e0d667532c60c841186dc","numbers_sha256":"2a388b387db82a1405d38b9b30a7fbb7deee74407cb7b56e4fd5f4932bda440b","store_sha256":"2c5444e37dd98ca8c1c2e17257dcd6e7ceecf03027fa42ddc7662e6db7caae62"},"lines7":20434,"children":[{"k":1,"area":"the primitives and the ways text comes in","root":"aa66104a","cid":"baguqeerajf5ogcvkv2hwe4ia7o36dek4ca6jpznau6y56p4qm3katr4itqea","car_bytes":239880,"blocks":1448,"lines":1424,"crumbs":24,"positions":1585,"crumbAt":[15,17,33,45,53,61,128,183,382,584,610,716,785,848,854,1040,1041,1098,1155,1278,1415,1445,1446,1447],"dur":48.409},{"k":2,"area":"ids that leave the process and come in","root":"dfcbfe22","cid":"baguqeeraorktmmuaob65teuqzuwq5tjlg4sxodt5mlhmfartdxco7trwjkfa","car_bytes":264343,"blocks":1630,"lines":1614,"crumbs":16,"positions":1704,"crumbAt":[11,16,224,421,614,804,809,928,1129,1297,1493,1496,1599,1609,1628,1629],"dur":34.199},{"k":3,"area":"the readers","root":"f96dce66","cid":"baguqeeraewrytmaiu7h2k7x5qdxp2xk2zp3xi6grr65gvyyc5nmkq2moamka","car_bytes":336813,"blocks":2027,"lines":2007,"crumbs":20,"positions":2130,"crumbAt":[14,16,25,38,246,448,502,696,894,1085,1281,1407,1600,1778,1964,2004,2005,2024,2025,2026],"dur":37.311},{"k":4,"area":"commands and display","root":"b3d92117","cid":"baguqeera5za3v4w6nredvv5z6infuky6nsxjzxkwbfifcdzhoxgwmebyhqcq","car_bytes":350968,"blocks":2243,"lines":2220,"crumbs":23,"positions":2316,"crumbAt":[18,20,29,37,245,447,649,681,767,804,875,884,1073,1275,1477,1679,1680,1882,2084,2214,2240,2241,2242],"dur":44.842},{"k":5,"area":"the Hermes side","root":"dedd912c","cid":"baguqeerakjkhmzzjlhxlrrqjmkpkxi5q246vee5amw6mmg4lu524hhjmk4oa","car_bytes":562666,"blocks":3341,"lines":3313,"crumbs":28,"positions":3476,"crumbAt":[18,20,30,40,250,452,654,856,1022,1221,1419,1618,1806,1811,1991,2146,2147,2338,2531,2537,2732,2934,3135,3188,3313,3338,3339,3340],"dur":53.968}],"totals":{"tokens_generated":34225,"tokens_sent_in_all_requests":1145776},"window":{"ace_max_in":163562,"ace_max_sent":114592,"ace_target":116000}}/*END*/;

  const S = 540, WX = x => (x - 960) / S, WY = y => (y - 540) / S, P = v => v / S;   // screen pixels at the full view -> world units
  const MONO = 'mono', CW = 0.605, fmt = n => n.toLocaleString('en-US');
  const kids = D.children, BIG = Math.max(...D.children.map(c => c.car_bytes)), N = D.lines7 + kids.reduce((a, c) => a + c.positions, 0);

  // the screen, in pixels: the children down the left, their CARs across, the lead's memory (level 7) at the right,
  // the gauge under them
  const ROW0 = 236, DY = 116, BX = 64, BW = 160, BH = 44, RX = 240, BEND = 780;
  const HX = 1400, HY = 468, HR = 640 / 1922, HPHI = 0.2;
  const into = () => at(WX(HX), WY(HY), HR, HPHI, center(me.k - 1));
  const rowY = i => WY(ROW0 + DY * i);
  // where the CAR of child i meets the lead's memory: the left edge of the screen window of the level below
  function landing(i) {
    const [cx, cy] = center(me.k - 1), [x, y] = ap(into(), cx - 1.78, cy + (i - 2) * 0.26);
    return [x - P(10), y];
  }
  const paths = [];
  function path(i) {
    if (paths[i]) return paths[i];
    const x0 = WX(RX), y0 = rowY(i), [x1, y1] = landing(i), xb = WX(BEND), n = 90, pts = [], cum = [0];
    for (let j = 0; j <= n; j++) {
      const x = lerp(x0, x1, j / n);
      pts.push([x, lerp(y0, y1, sm(clamp((x - xb) / (x1 - xb))))]);
      if (j) cum.push(cum[j - 1] + Math.hypot(pts[j][0] - pts[j - 1][0], pts[j][1] - pts[j - 1][1]));
    }
    return paths[i] = { pts, cum, len: cum[n] };
  }
  function along(p, s) {
    const L = clamp(s) * p.len; let j = 1;
    while (j < p.cum.length - 1 && p.cum[j] < L) j++;
    const a = p.pts[j - 1], b = p.pts[j], t = (L - p.cum[j - 1]) / ((p.cum[j] - p.cum[j - 1]) || 1), dx = b[0] - a[0], dy = b[1] - a[1], d = Math.hypot(dx, dy) || 1;
    return { x: lerp(a[0], b[0], t), y: lerp(a[1], b[1], t), dx: dx / d, dy: dy / d };
  }

  // when each CAR lands (u seconds): the order is that of the children's real ends; the time grows with the seconds the child lived
  const dmin = Math.min(...kids.map(c => c.dur)), dmax = Math.max(...kids.map(c => c.dur)), FLY = 0.6;
  const LAND = kids.map(c => 1.7 + 1.7 * (c.dur - dmin) / (dmax - dmin)), RANK = kids.map(c => kids.filter(o => o.dur < c.dur).length);

  // the gauge: each bar is one number of the dump, under its own name
  const G0 = 3.7, GSTEP = 0.15, GX = 64, GY = 822, GDY = 26, GSCALE = 1000 / D.totals.tokens_sent_in_all_requests, GFS = 15;
  const GAUGE = [
    ['tokens_sent_in_all_requests', D.totals.tokens_sent_in_all_requests, C.BLUE, 0.85],
    ['tokens_generated', D.totals.tokens_generated, C.TEAL, 0.9],
    ['ace_max_in', D.window.ace_max_in, C.GREY, 0.55],
    ['ace_max_sent', D.window.ace_max_sent, C.GREEN, 0.9],
    ['ace_target', D.window.ace_target, C.YELLOW, 0],
  ];

  const me = F.def({
    name: 'family', color: C.GOLD, R: 2.05, cx: 0, cy: 0,
    zoom: 2.2, dwell: 4.8, lines: N,
    hint: ['export_car → CID → import_car', 1.5],
    place: () => into(),
    draw(g, { u }) {
      kids.forEach((c, i) => {
        const y = rowY(i), a = seg(u, 0.1 + 0.08 * i, 0.6 + 0.08 * i), tl = LAND[i], dep = tl - FLY;
        g.text(c.area, WX(BX), y - P(40), { size: P(15), font: MONO, c: C.GREY, a: 0.9 * a, align: 'left', k: seg(u, 0.5 + 0.1 * i, 1.2 + 0.1 * i) });
        g.poly(rrectPts(WX(BX + BW / 2), y, P(BW / 2), P(BH / 2), P(6)), { c: C.GREY, w: P(1.5), a, closed: true, fill: C.GREY, fa: 0.07 });
        g.text(c.root, WX(BX + BW / 2), y, { size: P(20), font: MONO, c: C.WHITE, a, k: seg(u, 0.7 + 0.1 * i, 1.2 + 0.1 * i) });
        // the CAR: a band that grows from the child to the lead; its ticks are the crumb blocks, in the order of the file
        const fly = clamp((u - dep) / FLY), pr = sm(fly);
        if (fly > 0) {
          const p = path(i), after = seg(u, tl, tl + 0.9);
          g.poly(p.pts, { c: C.GOLD, w: P(2 + 4 * c.car_bytes / BIG), a: lerp(0.95, 0.5, after), k: pr });
          for (const idx of c.crumbAt) {
            const s = idx / (c.blocks - 1); if (s > pr) continue;
            const q = along(p, s), h = P(7);
            g.line(q.x + q.dy * h, q.y - q.dx * h, q.x - q.dy * h, q.y + q.dx * h, { c: C.YELLOW, w: P(1.3), a: 0.9 });
          }
          if (fly < 1) { const hd = along(p, pr); g.dot(hd.x, hd.y, P(5.5), C.WHITE, 0.95); }
          g.text(c.cid, WX(RX), y - P(14), { size: P(14), font: MONO, c: C.GREY, a: 0.85 * seg(u, dep - 0.1, dep + 0.2), align: 'left', k: seg(u, dep, dep + FLY) });
        }
        // what landed
        const na = seg(u, tl, tl + 0.5);
        if (na > 0) {
          const ny = y + P(26), f = 15, b = fmt(c.blocks), cb = fmt(c.car_bytes), x1 = RX + 150;
          g.text(b, WX(RX), ny, { size: P(f), font: MONO, c: C.WHITE, a: 0.9 * na, align: 'left' });
          g.text('blocks', WX(RX + b.length * f * CW + 12), ny, { size: P(f), font: MONO, c: C.GREY, a: 0.9 * na, align: 'left' });
          g.text(cb, WX(x1), ny, { size: P(f), font: MONO, c: C.WHITE, a: 0.9 * na, align: 'left' });
          g.text('car_bytes', WX(x1 + cb.length * f * CW + 12), ny, { size: P(f), font: MONO, c: C.GREY, a: 0.9 * na, align: 'left' });
        }
      });
      // the gauge
      const xT = GX + D.window.ace_target * GSCALE;
      g.line(WX(xT), WY(GY + GDY * 2 - 11), WX(xT), WY(GY + GDY * 4 + 11), { c: C.YELLOW, w: P(1), a: 0.55 * seg(u, G0 + 0.6, G0 + 1.0), dash: [0.008, 0.008] });
      GAUGE.forEach(([name, val, col, al], j) => {
        const y = WY(GY + GDY * j), t0 = G0 + GSTEP * j, wpx = val * GSCALE * seg(u, t0, t0 + 0.6), ta = seg(u, t0 + 0.35, t0 + 0.8), h = P(3.5);
        const box = [[WX(GX), y - h], [WX(GX + wpx), y - h], [WX(GX + wpx), y + h], [WX(GX), y + h]];
        if (wpx > 0.2) g.poly(box, al > 0 ? { c: col, w: 0, closed: true, fill: col, fa: al } : { c: col, w: P(1.4), closed: true, a: 0.9 });
        const lx = GX + val * GSCALE + 16;
        g.text(name, WX(lx), y, { size: P(GFS), font: MONO, c: col === C.GREY ? '#b4bac4' : col, a: ta, align: 'left' });
        g.text(fmt(val), WX(lx + name.length * GFS * CW + 16), y, { size: P(GFS), font: MONO, c: C.WHITE, a: 0.9 * ta, align: 'left' });
      });
    },
    // on top of the lead's memory: the same id at the other end, and the landing
    over(g, { u }) {
      kids.forEach((c, i) => {
        const tl = LAND[i], [lx, ly] = landing(i), f = (u - tl) / 0.5;
        g.text(c.root, lx, ly - P(15), { size: P(17), font: MONO, c: C.WHITE, a: seg(u, tl, tl + 0.3), align: 'right', k: seg(u, tl, tl + 0.4) });
        if (f > 0 && f < 1) flash(g, lx, ly, P(26), f, C.WHITE);
      });
      const [cx, cy] = center(me.k - 1), [x, y] = ap(into(), cx - 1.78, cy - 1);
      g.text('parent_store', x, y - P(14), { size: P(14), font: MONO, c: C.GREY, a: 0.85 * seg(u, 0.8, 1.4), align: 'left' });
    },
    tiny(g) {
      g.circle(0, 0, 0.22, { c: C.GOLD, w: 0.05, a: 0.9 });
      for (let i = 0; i < 5; i++) {
        const an = -Math.PI / 2 + TAU * i / 5;
        g.line(0.22 * Math.cos(an), 0.22 * Math.sin(an), 1.15 * Math.cos(an), 1.15 * Math.sin(an), { c: C.GOLD, w: 0.05, a: 0.7 });
        g.dot(1.3 * Math.cos(an), 1.3 * Math.sin(an), 0.12, C.GOLD, 0.9);
      }
    },
    ev: [
      ...LAND.map((t, i) => [t, 'wood', 3 + RANK[i], 0.7, -0.4 + 0.2 * i]),
      ...GAUGE.map((_, j) => j === 3 ? [G0 + GSTEP * j + 0.3, 'drum', 0, 0.8, 0] : [G0 + GSTEP * j + 0.3, 'tick', 2 + j, 0.16, 0]),
    ],
  });
})(Film);
