// v12 level 0, `line` (task F10). Plan section 5, level 0: "one real line, its bytes, its CRC32C". The one caption is  id = crc32c(bytes).
// The contract of a level is the comment at the top of film.js.
//
// In the frame: the hero line of the demo store, the one that levels 1 to 4 follow (line 3 of the text fd3130cf, store.json `line`):
//     3|Address = CRC32C of the bytes, as 8 hex digits. A different blob with the same
// One dot comes to its left end and reads it, byte by byte. Behind the dot each byte shows as its character and its two hex digits
// (one digit on each of two rows). Under them 32 squares are the real CRC32C register: all ones at the start, one step for each
// byte, then one flip of all 32 bits (the final xor). The dot ends on the newline byte. Then the address is struck, 2b5a7671, and
// the 32 squares are its bits: the four squares above a digit are that digit. No made-up text: each string drawn is one character
// of the line, one hex digit of its bytes, its id, or the caption. The register is computed in this file from the bytes, and the file
// throws when it loads if the bytes, the text and the id of the dump do not agree.
//
// DATA is store.json (sha256 2c5444e37dd98ca8c1c2e17257dcd6e7ceecf03027fa42ddc7662e6db7caae62): line.text, line.bytes_hex, line.id
// (= line.crc32c = levels["0"].ref), levels["0"].lines. The id of a line is BLUE, the colour of this level (as in levels/01.js).
//
// FIRST FRAME (u = 0): one dot, C.BLUE, radius 0.035 of a screen unit (18.9 px at 1080p), at the centre of the screen, nothing else (no
// text, no HUD, no lattice). The last level of the film closes on this same dot (F21, F31: the last frame equals the first).
//
// FOR THE JOIN (F30). The level is built on levels/01.js:
//   cx, cy     the centre of the disc is the centre of the TEXT ROW, so  at(HX, HY, K0, 0, center(0))  puts the row on (HX, HY).
//   R          1.1, which holds the row (it is 1.944 wide: 81 cells of 0.024) and everything under it.
//   K0         0.5 (the value in levels/01.js): the cell is then 0.012 wide, the width of a character of that page (0.6 * FS 0.02).
//              The row starts at x = -0.972 and ends at +0.972 in this frame, one cell for each of the 81 bytes (the last is the newline).
//   view       {x: 0, y: 0.13, v: 0.67}: the row fills 82 % of the screen, and the dot of the first frame is the centre of the screen.
//   at rest    (u >= 3.9, and u = 1e6): the row, its two rows of hex digits, the register (dim) and the id; the caption is written.
//   dwell      5.0 s. The level has a score of 32 events (a bell, a hat, 29 ticks, one wood block for the id).
(function (F) {
  'use strict';
  const { C, clamp, lerp, sm, seg, rrectPts } = F.kit;

  // ── the dump ────────────────────────────────────────────────────
  const TEXT = '3|Address = CRC32C of the bytes, as 8 hex digits. A different blob with the same\n';
  const HEX = '337c41646472657373203d20435243333243206f66207468652062797465732c206173203820686578206469676974732e204120646966666572656e7420626c6f622077697468207468652073616d650a';
  const ID = '2b5a7671';
  const LINES = 1;                                   // levels["0"].lines

  // ── CRC32C (Castagnoli, reflected, start and end with all ones). STATES[n] is the register after n bytes. ──
  const TAB = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0x82F63B78 : c >>> 1; return c >>> 0; });
  const BYTES = Array.from({ length: HEX.length / 2 }, (_, i) => parseInt(HEX.slice(2 * i, 2 * i + 2), 16));
  const STATES = [0xFFFFFFFF];
  for (const b of BYTES) { const s = STATES[STATES.length - 1]; STATES.push((TAB[(s ^ b) & 255] ^ (s >>> 8)) >>> 0); }
  const N = BYTES.length;                            // 81
  const FINAL = (STATES[N] ^ 0xFFFFFFFF) >>> 0;
  if (N !== TEXT.length || BYTES.some((b, i) => b !== TEXT.charCodeAt(i)) || FINAL.toString(16).padStart(8, '0') !== ID)
    throw new Error('levels/00.js: the bytes, the text and the id of the dump do not agree');

  // ── colours ─────────────────────────────────────────────────────
  const TXTC = '#d3d8e0', COOL = '#9aa0aa';          // the text of a line (as on the page of levels/01.js), and the newline dot
  const hex = c => [1, 3, 5].map(k => parseInt(c.slice(k, k + 2), 16));
  const mix = (a, b, t) => { const A = hex(a), B = hex(b); return '#' + A.map((v, k) => Math.round(lerp(v, B[k], t)).toString(16).padStart(2, '0')).join(''); };

  // ── the picture, in world units. At the full view one unit is 540 / VV = 806 px, so PX is one screen pixel. ──
  const VV = 0.67, PX = VV / 540, VY = 0.13;
  const P = 0.024, X0 = -N * P / 2, cellX = i => X0 + (i + 0.5) * P;     // cell i of the row (a mono letter is 0.6 of its size wide)
  const SZ = 0.04, SZ_HEX = 0.0215, SZ_ID = 0.087, ADV = 0.6 * SZ_ID, SQ = 0.0113;
  const Y_HI = 48 * PX, Y_LO = 68 * PX, Y_REG = 124 * PX, Y_ID = 190 * PX;   // the rows under the text row (y = 0)
  const DOT0 = 0.035 * VV, DOT1 = 0.011 * VV;                              // the first dot (18.9 px), and the dot that reads

  // ── the clock of this level, in seconds from the first frame ─────
  const DART0 = 1.0, DART1 = 1.45;                   // the dot goes from the centre of the screen to the left end of the row
  const A = 1.55, D = 1.15, CN = N + 1, U_END = A + D;   // the scan: its cell coordinate runs from 0 to 82 (cell i has its centre at i)
  const U_FLIP = 2.8, U_POP = 2.92;                  // the register flips, the address is struck
  const prof = f => lerp(f, sm(f), 0.25);            // a little slow at both ends
  const cOf = u => CN * prof(clamp((u - A) / D));
  const uOf = c => { let lo = 0, hi = 1; for (let n = 0; n < 40; n++) { const m = (lo + hi) / 2; if (prof(m) < c / CN) lo = m; else hi = m; } return A + D * (lo + hi) / 2; };
  const head = u => {
    if (u < A) { const s = sm((u - DART0) / (DART1 - DART0)); return [lerp(0, cellX(0), s), lerp(VY, 0, s)]; }
    return [cellX(Math.min(cOf(u), N - 1)), 0];
  };
  const headR = u => lerp(DOT0, DOT1, seg(u, DART0, DART1)) * (1 - 0.35 * seg(u, U_END, U_END + 0.3));

  // ── the score: a bell for the first dot; a tick for each three bytes that the dot reads, from left to right (the pan goes with
  // it); one tick for the flip; one wood block for the address that opens. score.py scales the whole film to its loudest sample,
  // so the strike is quiet (vel 0.4, as in levels/01.js) and the ticks are quieter still. ──
  const ev = [[0, 'bell', 0, 0.2, 0], [DART0, 'hat', 0, 0.08, -0.2], [DART1, 'tick', 0, 0.1, -0.7]];
  for (let i = 0; i < N; i += 3) ev.push([uOf(i), 'tick', 0, 0.03 + 0.04 * i / (N - 1), -0.8 + 1.6 * i / (N - 1)]);
  ev.push([U_FLIP, 'tick', 0, 0.12, 0], [U_POP, 'wood', 0, 0.4, 0]);

  F.def({
    name: 'line', color: C.BLUE, R: 1.1, cx: 0, cy: 0, dwell: 5.0, lines: LINES, view: { x: 0, y: VY, v: VV },
    hint: ['id = crc32c(bytes)', U_POP],
    draw(g, { u }) {
      const c = cOf(u), cc = c + 9 * seg(u, U_END, U_END + 0.3);   // cc goes on after the scan so that the last letters cool
      // far away the letters are no longer letters: the row is a bar
      const bar = 0.6 * (1 - clamp(SZ * g.s / 9));
      if (bar > 0.01) g.line(X0, 0, -X0, 0, { c: C.WHITE, w: 0.04, a: bar, k: clamp((c + 0.5) / N) });

      // the dot, and the streak of where it was a few ms ago; at the end of the row it is the newline byte
      const [hx, hy] = head(u), r = headR(u), m = seg(u, U_END, U_END + 0.4);
      for (let j = 0; j < 6; j++) {
        const p0 = head(u - 0.012 * j), p1 = head(u - 0.012 * (j + 1));
        if (Math.hypot(p0[0] - p1[0], p0[1] - p1[1]) < 0.0015) break;
        g.line(p1[0], p1[1], p0[0], p0[1], { c: C.BLUE, w: 2 * r * (1 - j / 7), a: 0.5 * (1 - j / 6) });
      }
      g.dot(hx, hy, r, C.BLUE, 1 - m);
      if (m > 0) g.dot(hx, hy, r, COOL, 0.8 * m);

      // the bytes: the character of each (BLUE as the dot passes, then it cools), and its two hex digits under it
      for (let i = 0; i < N && cc - i > 0; i++) {
        const ch = TEXT[i]; if (ch === ' ' || ch === '\n') continue;
        const heat = 1 - clamp((cc - i) / 7);
        g.text(ch, cellX(i), 0, { font: 'mono', size: SZ, c: heat > 0.004 ? mix(TXTC, C.BLUE, heat) : TXTC, a: 0.95 * sm((cc - i) / 1.6) });
      }
      for (let i = 0; i < N && cc - i > 1; i++) {
        const a = 0.85 * sm((cc - i - 1) / 1.6);
        g.text(HEX[2 * i], cellX(i), Y_HI, { font: 'mono', size: SZ_HEX, c: C.GREY, a });
        g.text(HEX[2 * i + 1], cellX(i), Y_LO, { font: 'mono', size: SZ_HEX, c: C.GREY, a });
      }

      // the register: 32 squares in 8 groups of 4, one group above each digit of the address. It comes in as the dot darts to
      // the row (all ones), follows the bytes, flips once, and stays under the address, dim.
      const ra = seg(u, DART0 + 0.1, DART1) * lerp(1, 0.5, seg(u, U_POP, U_POP + 0.5));
      if (ra > 0.004) {
        const s = STATES[clamp(Math.floor(c), 0, N)], f = seg(u, U_FLIP, U_FLIP + 0.06);
        for (let gi = 0; gi < 8; gi++) for (let j = 0; j < 4; j++) {
          const bit = 31 - 4 * gi - j, lit = lerp((s >>> bit) & 1, (FINAL >>> bit) & 1, f);
          g.poly(rrectPts((gi - 3.5) * ADV + (j - 1.5) * SQ, Y_REG, 0.44 * SQ, 0.44 * SQ, 0.12 * SQ),
            { c: C.BLUE, w: 0.0016, a: ra * (0.3 + 0.65 * lit), closed: true, fill: C.BLUE, fa: lit });
        }
      }

      // the address is struck: written across in 0.22 s, white-hot, and it cools to the colour of the level
      const q = seg(u, U_POP, U_POP + 0.22);
      if (q > 0) g.text(ID, 0, Y_ID, { font: 'mono', size: SZ_ID, c: mix(C.BLUE, '#ffffff', 0.85 * (1 - seg(u, U_POP, U_POP + 0.7))), k: q });
    },
    tiny(g) {
      g.line(X0, 0, -X0, 0, { c: C.WHITE, w: 0.05, a: 0.6 });
      g.line(-4 * ADV, Y_ID, 4 * ADV, Y_ID, { c: C.BLUE, w: 0.07, a: 0.85 });
    },
    ev,
  });
})(Film);
