// v12 film engine. Forked from Ascension (trikeshed/docs/video/ascension/film.js, read Nov 10 2026) with its sixteen
// levels removed. Film.frame(T) paints second T. Every level is drawn in its own frame; the level below lives inside
// it at a similarity placement, and the camera flies a logarithmic spiral from one frame's full view to the next.
//
// A level is one file, levels/NN.js, and one call: Film.def({ ... }). stubs/00.js and stubs/01.js are two working
// levels to copy from (level 0, and a level 1 that holds level 0 in eight cells).
//   name       ladder name                          color     its ladder tick and name, and the dot that stands for it under 5 px
//   R, cx, cy  the disc that holds the level: radius R, centre (cx, cy), in its own frame. Under tinyAt px (default 40) on the
//              screen, tiny() is drawn instead of draw() and the level below
//   lines      addressed lines in view at its full view, an integer >= 1 from the dump: the count in the corner
//   dwell      seconds at full view                 zoom      seconds of flight into it (every level but the first)
//   view       {x, y, v} camera framing at full view; default {0, 0, 1}, and then the screen is 3.56 x 2 world units
//   hint       [text, u] the one caption of the level, written in u seconds after the level's origin
//   place(T)   matrix that puts the level below inside this one (every level but the first). kit.at(x, y, r, phi, centre)
//              puts the point `centre` (kit.center(k) is level k's own) at (x, y), r times as big, turned phi radians
//   draw(g, {u, T, hero})   is painted first, then the level below through place(), then over(g, {u, T, hero}) on top
//   tiny(g, {u, T, hero})   what stands for the level when it is small    ev  [[u, kind, pitch, vel, pan], ...] for score.py
//              (kind: pluck, bell, chord, thump, kick, tick, hat, gliss; pitch is a step of the pentatonic scale)
//   close      true on the last level of a looping film only: no rung on the ladder, and the HUD fades out
//   optional   heroAlpha(u) fades the level below, turn(T) gives the spiral's angle, init(origin) runs once
// u is the time in seconds since the level's origin (the middle of its flight in). Film.def returns the level, and
// its .k is its place in the film, so a level draws the one below with kit.inst(g, me.k - 1, matrix, T).
// The page calls Film.build() once, after the level files have loaded; then Film.frame, Film.duration, Film.events,
// Film.curve(hz), Film.levels and Film.count(T) (the number in the corner at second T) are ready.
(function () {
  'use strict';
  const W = 1920, H = 1080, S = 540, TAU = Math.PI * 2;
  const C = {
    BLUE: '#58C4DD', BLUE_E: '#1C758A', TEAL: '#5CD0B3', GREEN: '#83C167', YELLOW: '#FFE45E', GOLD: '#F0AC5F',
    ORANGE: '#FF862F', RED: '#FC6255', PURPLE: '#9A72AC', PINK: '#D147BD', WHITE: '#FFFFFF', GREY: '#8A8F98',
  };
  const FONT = {
    math: 'KaTeXMath, KaTeXMain, KaTeXAMS, serif',
    main: 'KaTeXMain, KaTeXAMS, KaTeXMath, serif',
    mono: "'JetBrains Mono', 'DejaVu Sans Mono', monospace",
  };
  const MATHCH = /[αλ↻⋈∘⇄≠←→·]/;

  // ── numbers ───────────────────────────────────────────────────
  const clamp = (x, a = 0, b = 1) => x < a ? a : x > b ? b : x;
  const lerp = (a, b, t) => a + (b - a) * t;
  const sm = x => { x = clamp(x); return x * x * x * (x * (x * 6 - 15) + 10); };
  const seg = (u, a, b) => sm((u - a) / (b - a));
  const backOut = x => { x = clamp(x); const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2; };
  const pop = (u, a, d = 0.35) => u <= a ? 0 : backOut((u - a) / d);
  const tnb = x => x <= 0 || x >= 1 ? 0 : Math.sin(Math.PI * x);
  const frac = x => x - Math.floor(x);
  function rng(seed) {
    let s = seed >>> 0;
    return () => { s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  // ── affine maps [a b c d e f]: x' = a x + c y + e, y' = b x + d y + f ──
  const mul = (M, N) => [M[0] * N[0] + M[2] * N[1], M[1] * N[0] + M[3] * N[1], M[0] * N[2] + M[2] * N[3], M[1] * N[2] + M[3] * N[3],
    M[0] * N[4] + M[2] * N[5] + M[4], M[1] * N[4] + M[3] * N[5] + M[5]];
  const place = (x, y, r = 1, phi = 0) => { const c = Math.cos(phi) * r, s = Math.sin(phi) * r; return [c, s, -s, c, x, y]; };
  const ap = (M, x, y) => [M[0] * x + M[2] * y + M[4], M[1] * x + M[3] * y + M[5]];
  // place a frame so that its point (cx, cy) lands on (x, y)
  const at = (x, y, r, phi, [cx, cy]) => place(x - r * (Math.cos(phi) * cx - Math.sin(phi) * cy), y - r * (Math.sin(phi) * cx + Math.cos(phi) * cy), r, phi);
  const about = (x, y, s) => place(x * (1 - s), y * (1 - s), s, 0);

  // complex numbers as [re, im]
  const cmul = (a, b) => [a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0]];
  const cdiv = (a, b) => { const d = b[0] * b[0] + b[1] * b[1]; return [(a[0] * b[0] + a[1] * b[1]) / d, (a[1] * b[0] - a[0] * b[1]) / d]; };
  const cadd = (a, b) => [a[0] + b[0], a[1] + b[1]];
  const csub = (a, b) => [a[0] - b[0], a[1] - b[1]];
  const cexp = a => { const e = Math.exp(a[0]); return [e * Math.cos(a[1]), e * Math.sin(a[1])]; };
  const clog = a => [Math.log(Math.hypot(a[0], a[1])), Math.atan2(a[1], a[0])];

  // ── shapes ────────────────────────────────────────────────────
  const ngon = (n, r, rot = -Math.PI / 2, cx = 0, cy = 0) => Array.from({ length: n }, (_, i) => [cx + r * Math.cos(rot + TAU * i / n), cy + r * Math.sin(rot + TAU * i / n)]);
  const starPts = (n, r1, r2, rot = -Math.PI / 2) => Array.from({ length: 2 * n }, (_, i) => { const r = i % 2 ? r2 : r1, a = rot + Math.PI * i / n; return [r * Math.cos(a), r * Math.sin(a)]; });
  function resample(pts, N) {
    const P = pts.concat([pts[0]]), L = [0];
    for (let i = 1; i < P.length; i++) L.push(L[i - 1] + Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]));
    const out = [], tot = L[L.length - 1];
    for (let k = 0, j = 1; k < N; k++) {
      const s = tot * k / N;
      while (L[j] < s) j++;
      const t = (s - L[j - 1]) / (L[j] - L[j - 1] || 1);
      out.push([lerp(P[j - 1][0], P[j][0], t), lerp(P[j - 1][1], P[j][1], t)]);
    }
    return out;
  }
  function rrectPts(cx, cy, hw, hh, r) {
    const pts = [];
    for (const [x, y, a0] of [[cx + hw - r, cy - hh + r, -Math.PI / 2], [cx + hw - r, cy + hh - r, 0], [cx - hw + r, cy + hh - r, Math.PI / 2], [cx - hw + r, cy - hh + r, Math.PI]])
      for (let k = 0; k <= 4; k++) { const a = a0 + Math.PI / 2 * k / 4; pts.push([x + r * Math.cos(a), y + r * Math.sin(a)]); }
    return pts;
  }
  const ellPts = (cx, cy, rx, ry, rot, a0, a1, n = 48) => Array.from({ length: n + 1 }, (_, i) => {
    const a = lerp(a0, a1, i / n), x = rx * Math.cos(a), y = ry * Math.sin(a);
    return [cx + x * Math.cos(rot) - y * Math.sin(rot), cy + x * Math.sin(rot) + y * Math.cos(rot)];
  });

  // ── drawing context: world units in, pixels out ───────────────
  class G {
    constructor(ctx) { this.ctx = ctx; this.M = [1, 0, 0, 1, 0, 0]; this.a = 1; this.tint = null; this.st = []; }
    push(N, a = 1, tint) { this.st.push([this.M, this.a, this.tint]); this.M = mul(this.M, N); this.a *= a; if (tint !== undefined) this.tint = tint; }
    pop() { [this.M, this.a, this.tint] = this.st.pop(); }
    get s() { const M = this.M; return Math.sqrt(Math.abs(M[0] * M[3] - M[1] * M[2])); }
    col(c) { return this.tint || c; }
    set() { const M = this.M; this.ctx.setTransform(M[0], M[1], M[2], M[3], M[4], M[5]); }
    // a sub-pixel stroke fades with its coverage instead of vanishing
    stroke(c, w, a) {
      const px = w * this.s, ctx = this.ctx;
      if (px < 1) { ctx.lineWidth = 1 / this.s; ctx.globalAlpha = clamp(a * this.a * Math.max(px, 0.04)); }
      else { ctx.lineWidth = w; ctx.globalAlpha = clamp(a * this.a); }
      ctx.strokeStyle = this.col(c);
    }
    line(x1, y1, x2, y2, o = {}) {
      const { c = C.WHITE, w = 0.006, a = 1, k = 1, dash = null } = o;
      if (a * this.a <= 0.004 || k <= 0) return;
      const ctx = this.ctx; this.set(); this.stroke(c, w, a); ctx.lineCap = 'round';
      if (dash) ctx.setLineDash(dash);
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(lerp(x1, x2, k), lerp(y1, y2, k)); ctx.stroke();
      if (dash) ctx.setLineDash([]);
    }
    poly(pts, o = {}) {
      const { c = C.WHITE, w = 0.006, a = 1, k = 1, closed = false, fill = null, fa = 0.2, dash = null } = o;
      if (a * this.a <= 0.004 || k <= 0 || pts.length < 2) return;
      const ctx = this.ctx; this.set();
      if (fill && fa > 0 && k >= 1) {
        ctx.globalAlpha = clamp(fa * a * this.a); ctx.fillStyle = this.col(fill);
        ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); ctx.closePath(); ctx.fill();
      }
      if (!(w > 0) || !c) return;
      this.stroke(c, w, a); ctx.lineJoin = 'round'; ctx.lineCap = 'round';
      if (dash) ctx.setLineDash(dash);
      ctx.beginPath();
      if (k >= 1) { pts.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])); if (closed) ctx.closePath(); }
      else {
        const P = closed ? pts.concat([pts[0]]) : pts, L = [];
        let tot = 0;
        for (let i = 1; i < P.length; i++) { const d = Math.hypot(P[i][0] - P[i - 1][0], P[i][1] - P[i - 1][1]); L.push(d); tot += d; }
        let rem = tot * k; ctx.moveTo(P[0][0], P[0][1]);
        for (let i = 1; i < P.length && rem > 0; i++) {
          const d = L[i - 1];
          if (d <= rem) { ctx.lineTo(P[i][0], P[i][1]); rem -= d; }
          else { const t = rem / d; ctx.lineTo(lerp(P[i - 1][0], P[i][0], t), lerp(P[i - 1][1], P[i][1], t)); rem = 0; }
        }
      }
      ctx.stroke();
      if (dash) ctx.setLineDash([]);
    }
    circle(x, y, r, o = {}) {
      const { c = C.WHITE, w = 0.006, a = 1, k = 1, fill = null, fa = 1, a0 = -Math.PI / 2, dash = null } = o;
      if (a * this.a <= 0.004 || k <= 0 || r <= 0) return;
      const ctx = this.ctx, pr = r * this.s; this.set();
      if (pr < 0.6) {
        if (!fill && !c) return;
        ctx.globalAlpha = clamp(a * this.a * (fill ? fa : 0.5) * Math.max(pr / 0.6, 0.15) ** 2 * 1.5);
        ctx.fillStyle = this.col(fill || c); const q = 0.6 / this.s; ctx.fillRect(x - q, y - q, 2 * q, 2 * q); return;
      }
      if (fill && fa > 0) { ctx.globalAlpha = clamp(fa * a * this.a * Math.min(1, k * 1.5)); ctx.fillStyle = this.col(fill); ctx.beginPath(); ctx.arc(x, y, r, 0, TAU); ctx.fill(); }
      if (w > 0 && c) {
        this.stroke(c, w, a); if (dash) ctx.setLineDash(dash);
        ctx.beginPath(); ctx.arc(x, y, r, a0, a0 + TAU * k); ctx.stroke();
        if (dash) ctx.setLineDash([]);
      }
    }
    dot(x, y, r, c, a = 1) { this.circle(x, y, r, { c: null, w: 0, fill: c, fa: 1, a }); }
    // text sized in world units; Greek and math symbols in a mono string are set in Computer Modern
    text(str, x, y, o = {}) {
      const { size = 0.08, font = 'math', c = C.WHITE, a = 1, align = 'center', k = 1 } = o;
      if (a * this.a <= 0.004 || k <= 0) return;
      const s = this.s, px = size * s; if (px < 3.2) return;
      const M = this.M, [X, Y] = ap(M, x, y), ctx = this.ctx;
      ctx.setTransform(M[0] / s, M[1] / s, M[2] / s, M[3] / s, X, Y);
      ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
      const runs = [];
      if (font === 'mono') { let cur = null; for (const ch of str) { const f = MATHCH.test(ch) ? 'math' : 'mono'; if (!cur || cur[1] !== f) runs.push(cur = ['', f]); cur[0] += ch; } }
      else runs.push([str, font]);
      let wd = 0;
      for (const r of runs) { ctx.font = `${px.toFixed(2)}px ${FONT[r[1]]}`; r[2] = ctx.measureText(r[0]).width; wd += r[2]; }
      const x0 = align === 'center' ? -wd / 2 : align === 'right' ? -wd : 0;
      const alpha = clamp(a * this.a * clamp(px / 7 - 0.3));
      const paint = (stroke) => { let xx = x0; for (const r of runs) { ctx.font = `${px.toFixed(2)}px ${FONT[r[1]]}`; if (stroke) ctx.strokeText(r[0], xx, 0); else ctx.fillText(r[0], xx, 0); xx += r[2]; } };
      ctx.fillStyle = ctx.strokeStyle = this.col(c);
      if (k >= 1) { ctx.globalAlpha = alpha; paint(false); return; }
      // Write: an outline sweeps across, the fill follows
      ctx.save(); ctx.beginPath(); ctx.rect(x0 - px, -px, (wd + 2 * px) * clamp(k * 1.5), 2 * px); ctx.clip();
      ctx.globalAlpha = alpha * (1 - clamp((k - 0.55) / 0.45)); ctx.lineWidth = Math.max(0.8, px / 30); paint(true);
      ctx.restore();
      ctx.globalAlpha = alpha * clamp((k - 0.3) / 0.7); paint(false);
    }
  }

  // ── small vocabulary of marks ────────────────────────────────
  function bowtie(g, x, y, hw, hh, o = {}) {
    const { c = C.WHITE, w = 0.006, k = 1, a = 1, fa = 0.12 } = o;
    g.poly([[x - hw, y - hh], [x - hw, y + hh], [x, y]], { c, w, k, a, closed: true, fill: c, fa });
    g.poly([[x + hw, y - hh], [x + hw, y + hh], [x, y]], { c, w, k, a, closed: true, fill: c, fa });
  }
  function arrowHead(g, x, y, ang, s, c, a = 1) {
    g.poly([[x + s * Math.cos(ang), y + s * Math.sin(ang)], [x + 0.62 * s * Math.cos(ang + 2.45), y + 0.62 * s * Math.sin(ang + 2.45)], [x + 0.62 * s * Math.cos(ang - 2.45), y + 0.62 * s * Math.sin(ang - 2.45)]],
      { c, w: 0, closed: true, fill: c, fa: 1, a });
  }
  function flash(g, x, y, r, p, c, n = 12) {
    if (p <= 0 || p >= 1) return;
    for (let i = 0; i < n; i++) {
      const an = TAU * i / n, r0 = r * (0.45 + 0.75 * p), r1 = r * (0.75 + 1.0 * p);
      g.line(x + r0 * Math.cos(an), y + r0 * Math.sin(an), x + r1 * Math.cos(an), y + r1 * Math.sin(an), { c, w: r * 0.06, a: (1 - p) * Math.min(1, p * 6) });
    }
  }
  function keyGlyph(g, x, y, s, rot, c, a = 1) {
    g.push(place(x, y, s, rot));
    g.circle(-0.42, 0, 0.24, { c, w: 0.1, a });
    g.line(-0.18, 0, 0.58, 0, { c, w: 0.1, a });
    g.line(0.3, 0, 0.3, 0.22, { c, w: 0.1, a });
    g.line(0.5, 0, 0.5, 0.28, { c, w: 0.1, a });
    g.pop();
  }

  // ── level registry and the recursive painter ─────────────────
  const LV = [];
  const def = o => {
    if (built) throw new Error('Film.def after Film.build');
    o.view = Object.assign({ x: 0, y: 0, v: 1 }, o.view || {}); o.tinyAt = o.tinyAt || 40; o.k = LV.length; LV.push(o); return o;
  };
  const origin = [];
  let canvasW = W, canvasH = H, latticeGain = 1;

  function lattice(g, L) {
    const s = g.s, vis = clamp((s - 170) / 260) * clamp((3400 - s) / 1700) * latticeGain;
    if (vis <= 0.01) return;
    const ctx = g.ctx, R = L.R * 1.7, sp = 0.25, cx = L.cx, cy = L.cy;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#3d6f86';
    const n = Math.ceil(R / sp);
    for (let i = -n; i <= n; i++) for (let j = -n; j <= n; j++) {
      const x = Math.round(cx / sp) * sp + i * sp, y = Math.round(cy / sp) * sp + j * sp, d = Math.hypot(x - cx, y - cy) / R;
      if (d >= 1) continue;
      const [X, Y] = ap(g.M, x, y);
      if (X < -4 || Y < -4 || X > canvasW + 4 || Y > canvasH + 4) continue;
      ctx.globalAlpha = 0.3 * vis * (1 - d * d) * g.a;
      ctx.fillRect(X - 1.1, Y - 1.1, 2.2, 2.2);
    }
  }

  const sub = (k, st) => st.hero ? { u: st.T - origin[k], T: st.T, hero: true } : { u: 1e6, T: st.T, hero: false };
  function drawLevel(k, g, st) {
    const L = LV[k], px = g.s * L.R;
    if (px < 0.7 || g.a <= 0.004) return;
    const [X, Y] = ap(g.M, L.cx, L.cy), rr = px * 1.15;
    if (X + rr < 0 || X - rr > canvasW || Y + rr < 0 || Y - rr > canvasH) return;
    if (px < 5) { const r = Math.min(px, 3.2) * 0.5 / g.s; g.dot(L.cx, L.cy, r, L.color, clamp(px / 5) * 0.9); return; }
    if (px < L.tinyAt) { L.tiny(g, st); return; }
    if (st.hero) lattice(g, L);
    if (L.draw) L.draw(g, st);
    if (L.place && k > 0) {
      g.push(L.place(st.T), L.heroAlpha ? L.heroAlpha(st.u) : 1);
      drawLevel(k - 1, g, sub(k - 1, st));
      g.pop();
    }
    if (L.over) L.over(g, st);
  }
  function inst(g, k, M, T, a = 1) { g.push(M, a); drawLevel(k, g, { u: 1e6, T, hero: false }); g.pop(); }
  const center = k => [LV[k].cx, LV[k].cy];

  // ── timeline: made by build(), once, after the level files have called Film.def ──
  const UNIT = 'line';
  const DRIFT = 0.06, ZS = [], ZE = [], DE = [];
  let LAST = 0, CLOSES = false, RUNGS = 0, duration = 0, NAMES = [], LOGN = [], built = false;
  const events = [];
  function build() {
    if (built) throw new Error('Film.build ran twice');
    if (!LV.length) throw new Error('Film.build: no level was defined (a levels/NN.js file calls Film.def)');
    LV.forEach((L, k) => {
      const bad = what => { throw new Error(`level ${k} '${L.name}': ${what}`); };
      if (typeof L.name !== 'string' || !L.name || typeof L.color !== 'string') bad('needs name and color');
      if (![L.R, L.cx, L.cy].every(Number.isFinite) || !(L.R > 0)) bad('needs R > 0, cx, cy');
      if (!(L.dwell > 0) || (k > 0 && !(L.zoom > 0))) bad('needs dwell > 0, and zoom > 0 unless it is the first level');
      if (!Number.isInteger(L.lines) || L.lines < 1) bad('needs lines, an integer >= 1');
      if (typeof L.draw !== 'function' || typeof L.tiny !== 'function') bad('needs draw() and tiny()');
      if (k > 0 && typeof L.place !== 'function') bad('needs place(): the matrix that puts the level below inside it');
      if (L.close && k < LV.length - 1) bad('close belongs to the last level only');
    });
    ZS[0] = -1; ZE[0] = 0; DE[0] = LV[0].dwell; origin[0] = 0;
    for (let k = 1, t = LV[0].dwell; k < LV.length; k++) {
      ZS[k] = t; ZE[k] = t + LV[k].zoom; DE[k] = ZE[k] + LV[k].dwell; origin[k] = ZS[k] + 0.5 * LV[k].zoom; t = DE[k];
    }
    LAST = LV.length - 1; duration = DE[LAST]; CLOSES = !!LV[LAST].close; RUNGS = CLOSES ? LAST : LAST + 1;
    NAMES = LV.map(L => L.name); LOGN = LV.map(L => Math.log10(L.lines));
    LV.forEach((L, k) => { if (L.init) L.init(origin[k]); });
    // score data: events on the global clock, and the ascent curve
    LV.forEach((L, k) => {
      const list = typeof L.ev === 'function' ? L.ev(origin[k]) : (L.ev || []);
      for (const [u, kind, p, vel, pan] of list) { const t = origin[k] + u; if (t >= 0 && t < duration) events.push({ t, kind, p, vel, pan: pan || 0, level: k }); }
      if (k > 0) events.push({ t: ZE[k], kind: 'thump', p: 0, vel: 0.3, pan: 0, level: k });
    });
    events.sort((a, b) => a.t - b.t);
    built = true;
    Object.assign(Film, { duration, events, curve, levels: LV.map((L, k) => ({ name: L.name, lines: L.lines, zs: ZS[k], ze: ZE[k], de: DE[k], origin: origin[k] })) });
  }
  const curve = hz => Array.from({ length: Math.ceil(duration * hz) + 1 }, (_, i) => levelAt(i / hz));
  function levelAt(T) {
    if (T < DE[0]) return DRIFT * T / DE[0];
    for (let k = 1; k <= LAST; k++) {
      if (T < ZE[k]) return k - 1 + DRIFT + (1 - DRIFT) * sm((T - ZS[k]) / LV[k].zoom);
      if (T < DE[k]) return k === LAST ? LAST : k + DRIFT * (T - ZE[k]) / LV[k].dwell;
    }
    return LAST;
  }
  function camera(T, q) {
    if (LAST === 0) {   // one level alone: no flight, only its own framing
      const v = LV[0].view, A = S * q / v.v;
      return { a: 0, Lv: 0, M: [A, 0, 0, A, W * q / 2 - A * v.x, H * q / 2 - A * v.y] };
    }
    const Lv = levelAt(T), lo = Math.min(Math.floor(Lv), LAST - 1), t = Lv - lo, a = lo + 1;
    const P = LV[a].place(T), rho = [P[0], P[1]], c = [P[4], P[5]], vp = LV[a - 1].view, va = LV[a].view;
    // the spiral turns through the placement's own angle, unwrapped where it moves with time
    const phi = LV[a].turn ? LV[a].turn(T) : Math.atan2(P[1], P[0]), r = Math.hypot(P[0], P[1]);
    const l0 = cmul(rho, [vp.v, 0]), Q0 = cadd(c, cmul(rho, [vp.x, vp.y])), l1 = [va.v, 0], Q1 = [va.x, va.y];
    const lg = [Math.log(va.v / (r * vp.v)), -phi], lt = cmul(l0, cexp([lg[0] * t, lg[1] * t]));
    const p = cdiv(csub(cmul(Q1, l0), cmul(Q0, l1)), csub(l0, l1)), Q = cadd(p, cmul(csub(Q0, p), cdiv(lt, l0)));
    const A = cdiv([S * q, 0], lt), B = csub([W * q / 2, H * q / 2], cmul(A, Q));
    return { a, Lv, M: [A[0], A[1], -A[1], A[0], B[0], B[1]] };
  }

  // ── HUD: the ladder climbed so far, and how many lines are in view ──
  const SUP = '⁰¹²³⁴⁵⁶⁷⁸⁹';
  function hudAlpha(T) { return seg(T, 1.0, 2.0) * (CLOSES ? 1 - seg(T - origin[LAST], 3.0, 4.0) : 1); }
  // the lines in view: exactly the level's own number while it holds, log-interpolated through each flight into the next
  function countAt(Lv) {
    if (LAST === 0) return LV[0].lines;
    const lo = Math.min(Math.floor(Lv), LAST - 1), f = Lv - lo;
    if (f <= DRIFT) return LV[lo].lines;
    if (f >= 1) return LV[lo + 1].lines;
    return Math.round(10 ** lerp(LOGN[lo], LOGN[lo + 1], (f - DRIFT) / (1 - DRIFT)));
  }
  function runs(ctx, str, x, y, px, align, mono) {
    const parts = []; let cur = null;
    for (const ch of str) { const f = MATHCH.test(ch) ? FONT.math : mono; if (!cur || cur[1] !== f) parts.push(cur = ['', f]); cur[0] += ch; }
    let wd = 0; for (const p of parts) { ctx.font = `${px}px ${p[1]}`; p[2] = ctx.measureText(p[0]).width; wd += p[2]; }
    let xx = align === 'right' ? x - wd : x;
    for (const p of parts) { ctx.font = `${px}px ${p[1]}`; ctx.fillText(p[0], xx, y); xx += p[2]; }
  }
  function hud(ctx, T, Lv, q) {
    const a = hudAlpha(T); if (a <= 0.003) return;
    ctx.setTransform(q, 0, 0, q, 0, 0); ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    const X = 1866, Y0 = 950, Y1 = 130, top = Math.max(1, RUNGS - 1), yOf = l => lerp(Y0, Y1, l / top), cur = Math.min(Lv, RUNGS - 1);
    ctx.globalAlpha = 0.16 * a; ctx.fillStyle = '#ffffff'; ctx.fillRect(X - 1, Y1, 2, Y0 - Y1);
    ctx.globalAlpha = 0.55 * a; ctx.fillRect(X - 1, yOf(cur), 2, Y0 - yOf(cur));
    for (let l = 0; l < RUNGS; l++) {
      if (l > cur + 0.35) continue;
      const on = Math.abs(l - cur) < 0.5, y = yOf(l), la = clamp(1 - Math.abs(l - cur) * 1.6);
      ctx.globalAlpha = (on ? 1 : 0.4) * a; ctx.fillStyle = on ? LV[l].color : '#9aa0aa';
      ctx.fillRect(X - 6, y - 1, 12, 2);
      if (la > 0.01) { ctx.globalAlpha = la * a; ctx.fillStyle = LV[l].color; runs(ctx, NAMES[l], X - 16, y, 18, 'right', FONT.mono); }
    }
    const my = yOf(cur); ctx.globalAlpha = a; ctx.fillStyle = LV[Math.round(cur)].color;
    ctx.beginPath(); ctx.moveTo(X + 6, my); ctx.lineTo(X + 18, my - 7); ctx.lineTo(X + 18, my + 7); ctx.closePath(); ctx.fill();
    const n = countAt(Lv), e = Math.floor(Math.log10(n)), m = n / 10 ** e;
    ctx.globalAlpha = 0.8 * a; ctx.fillStyle = '#ffffff'; ctx.font = `34px ${FONT.main}`;
    const body = e < 5 ? `${UNIT} × ${Math.round(n).toLocaleString('en-US')}` : `${UNIT} × ${m.toFixed(1)} · 10`;
    ctx.fillText(body, 48, 64);
    if (e >= 5) { const w = ctx.measureText(body).width; ctx.font = `22px ${FONT.main}`; ctx.fillText(String(e).split('').map(d => SUP[+d]).join(''), 50 + w, 52); }
    // the engineers' caption: one line of the real names, written in, gone at the next climb
    for (let k = 0; k < LV.length; k++) {
      const h = LV[k].hint; if (!h) continue;
      const kw = seg(T - origin[k], h[1], h[1] + 0.9), out = 1 - seg(T, DE[k], DE[k] + 0.5);
      if (kw <= 0 || out <= 0) continue;
      ctx.save(); ctx.beginPath(); ctx.rect(40, 990, 1700 * clamp(kw * 1.15), 80); ctx.clip();
      ctx.globalAlpha = 0.85 * a * out; ctx.fillStyle = '#b4bac4'; runs(ctx, h[0], 48, 1032, 24, 'left', FONT.mono);
      ctx.restore();
    }
  }

  // ── frame ─────────────────────────────────────────────────────
  let cv, ctx, b1, b2;
  function bind(canvas) {
    cv = canvas; ctx = cv.getContext('2d'); canvasW = cv.width; canvasH = cv.height;
    const mk = (w, h) => { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; };
    b1 = mk(cv.width >> 1, cv.height >> 1); b2 = mk(cv.width >> 2, cv.height >> 2);
  }
  function frame(T) {
    if (!built) throw new Error('Film.frame before Film.build');
    T = clamp(T, 0, duration);
    const q = cv.width / W, cam = camera(T, q), g = new G(ctx);
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, cv.width, cv.height);
    g.M = cam.M; latticeGain = seg(T, 0.8, 2.0) * (CLOSES ? 1 - seg(T - origin[LAST], 2.0, 3.0) : 1);
    drawLevel(cam.a, g, { u: T - origin[cam.a], T, hero: true });
    // bloom: two blurred copies added back
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = 1;
    const c1 = b1.getContext('2d'), c2 = b2.getContext('2d');
    c1.globalCompositeOperation = 'copy'; c1.filter = 'blur(3px)'; c1.drawImage(cv, 0, 0, b1.width, b1.height);
    c2.globalCompositeOperation = 'copy'; c2.filter = 'blur(6px)'; c2.drawImage(cv, 0, 0, b2.width, b2.height);
    ctx.globalCompositeOperation = 'lighter'; ctx.globalAlpha = 0.5; ctx.drawImage(b1, 0, 0, cv.width, cv.height);
    ctx.globalAlpha = 0.45; ctx.drawImage(b2, 0, 0, cv.width, cv.height);
    ctx.globalCompositeOperation = 'source-over'; ctx.globalAlpha = 1;
    const vg = ctx.createRadialGradient(cv.width / 2, cv.height / 2, cv.height * 0.45, cv.width / 2, cv.height / 2, cv.width * 0.62);
    vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(0,0,0,0.55)'); ctx.fillStyle = vg; ctx.fillRect(0, 0, cv.width, cv.height);
    hud(ctx, T, cam.Lv, q);
  }

  const kit = {
    W, H, S, TAU, C, FONT, clamp, lerp, sm, seg, backOut, pop, tnb, frac, rng, mul, place, ap, at, about,
    cmul, cdiv, cadd, csub, cexp, clog, ngon, starPts, resample, rrectPts, ellPts, bowtie, arrowHead, flash, keyGlyph,
    drawLevel, inst, sub, center,
  };
  const Film = window.Film = { def, kit, build, bind, frame, count: T => countAt(levelAt(T)) };
})();
