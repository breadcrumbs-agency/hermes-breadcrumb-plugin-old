// Level 9, `harness` (F19): four programs round one format. Claude Code, Codex, OpenCode and Hermes each write their own store.
// Data: take A of I07, four-run.json of runs/20261110-174111 in the breadcrumbs-hive repo (a copy is v12/four-run.json,
// sha256 089bc68f4c39487174dcef6de9ef5ee68b709cec103759905d71b62a1003eaa7). RUN below is that file, cut to the keys that are drawn.
// Every string that this level draws is a key, a text or a number of that file, or a part of one. The only numbers that do not come
// from it are the 65 tick lengths in LEN: the byte lengths of the lines of work/crc32c.py (1672 bytes, sha256 7d7d79e9af3872a9...),
// the file that the programs read. Layout numbers (positions, sizes, seconds) are drawing, not data.
// lines = 65 = line_id.lines: the 65 addressed lines of that file. Each program that ran holds all 65 in its store
// (lines_found_after_joins); the same line has the same id in every store, so it is one line in view, not three.
// 3 of 4 programs ran. Codex mounted the server, its account was at its usage limit and no model ran: its pane is drawn with its own
// error text, as it was, and it has no strike, no crumb and no verify. Hermes was driven by a script, with no model
// (programs.hermes.driver): that text is drawn too. In the Hermes pane the second sha256 ("recorded") is what the pane printed: the
// same text without its final newline, so its sha256 differs; the store holds the same bytes as in the other two panes.
// The level below (family) sits in the middle, at place(): 0.19 of its size, turned 0.3 rad, centred at (0, -0.02): a disc of radius
// 0.30 (stub 01, R 1.6) or 0.39 (level 8, R 2.05). With level 8 below, what it draws stays between the id (y -0.43) and the bars
// (y 0.355) at the full view (checked on a still at 70.9 s of the stack 00 to 09).
// Sound (as F04 gives it): a wood block for each address that opens (three: one for each store), none for Codex (nothing opens);
// no drum (a drum is for a fold); a short flute line in the flight in.
(function (F) {
  'use strict';
  const { C, seg, rrectPts, at, center, flash, arrowHead } = F.kit;

  const RUN = {
    "programs": {
      "claude-code": {
        "status": "done",
        "client": {
          "name": "claude-code",
          "title": "Claude Code",
          "version": "2.1.311",
          "protocol": "2025-11-25"
        }
      },
      "codex": {
        "status": "not_run",
        "client": {
          "name": "codex-mcp-client",
          "title": "Codex",
          "version": "0.145.0",
          "protocol": "2025-06-18"
        },
        "reason": "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at Nov 11th, 2026 9:11 AM."
      },
      "opencode": {
        "status": "done",
        "client": {
          "name": "opencode",
          "title": null,
          "version": "1.18.30",
          "protocol": "2025-11-25"
        }
      },
      "hermes": {
        "status": "done",
        "client": null,
        "driver": "a script, no model; the memory provider through agent.memory_manager.MemoryManager"
      }
    },
    "line_id": {
      "file": "crc32c.py",
      "line": 12,
      "text": "_POLY = 0x82F63B78",
      "id": "1721f9ac",
      "lines": 65
    },
    "crumb": {
      "id": "197d1069",
      "sha256": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
      "per_program": {
        "claude-code": {
          "store_view": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "sent": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "recorded": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "recorded_note": ""
        },
        "opencode": {
          "store_view": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "sent": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "recorded": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "recorded_note": ""
        },
        "hermes": {
          "store_view": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "sent": "937d97de30c869c52fdbfad5ce0c1b1d97ad6dcbcdc2b07219223550b6944f77",
          "recorded": "88a6b2bad35b175f40ec6e025f48ae63161d4ceb0641d4dd5995a65734d48b7e",
          "recorded_note": "the same text without its final newline (the core's plain() unwraps the JSON result of the provider and drops one)"
        }
      }
    },
    "verify": {
      "claude-code": {
        "blobs": 238,
        "bad": 0
      },
      "opencode": {
        "blobs": 225,
        "bad": 0
      },
      "hermes": {
        "blobs": 222,
        "bad": 0
      }
    },
    "times": {
      "panes": {
        "claude-code": {
          "start": 0.0,
          "program_s": 22.1916
        },
        "opencode": {
          "start": 0.0612,
          "program_s": 37.6909
        },
        "hermes": {
          "start": 0.0708,
          "program_s": 0.1334
        }
      },
      "overlap": {
        "together_s": 22.126,
        "sum_s": 59.883,
        "wall_s": 37.756
      },
      "lock_wait_ms": 0.075
    }
  };
  const LEN = [53, 0, 63, 61, 40, 0, 73, 78, 48, 3, 0, 18, 0, 23, 0, 0, 18, 14, 24, 13, 26, 53, 23, 23, 0, 0, 22, 0, 0, 29, 59, 29, 18, 24, 18, 44, 25, 0, 0, 14, 90, 34, 8, 44, 0, 47, 43, 61, 0, 49, 21, 12, 8, 45, 0, 36, 48, 0, 35, 21, 12, 30, 0, 0, 27];

  const KEYS = ['claude-code', 'codex', 'opencode', 'hermes'];     // programs_wanted: the order of the take; 0 and 1 are the top row
  const CW = 1.0, CH = 0.70, X0 = [-1.42, 0.42], Y0 = [-0.74, 0.06];
  const cellAt = i => ({ x: X0[i % 2], y: Y0[i >> 1] });
  const TK = { x: 0.05, y: 0.125, pitch: 0.0075, unit: 0.0032, w: 0.0045 };   // the strip of lines in a pane
  const ROW = RUN.line_id.line - 1, ID = RUN.line_id.id, BYTES = RUN.line_id.text, CR = RUN.crumb;
  const sha16 = s => s.slice(0, 16);
  const wrap = (s, n) => {
    const out = []; let cur = '';
    for (const w of s.split(/\s+/)) { if (cur && cur.length + 1 + w.length > n) { out.push(cur); cur = w; } else cur = cur ? cur + ' ' + w : w; }
    if (cur) out.push(cur);
    return out;
  };
  const wr = (u, t, d = 0.5) => seg(u, t, t + d);                  // write-in progress of a text that starts at second t
  const T = (g, s, x, y, size, c, align, k, a = 1) => g.text(s, x, y, { size, font: 'mono', c, align, k, a });

  // when each beat happens, in seconds since the level's origin: the flight in is u = -1.1 to 1.1, full view from 1.1 to 5.9
  const BEAT = { ticks: 0.3, bytes: 0.5, idC: 0.9, codex: 2.2, crumb: 3.2, rec: 3.6, verify: 3.7, bars: 4.0 };
  const STRIKE = { 'claude-code': 1.25, opencode: 1.55, hermes: 1.85 };   // line 12 opens in a store
  const RAN = KEYS.filter(k => RUN.programs[k].status === 'done');

  function ranPane(g, u, key, P, x, y) {
    const j = RAN.indexOf(key), ts = STRIKE[key], ty = n => y + TK.y + n * TK.pitch, xl = x + 0.5;
    // the lines of the file, one tick each; its length is the length of the line
    for (let n = 0; n < LEN.length; n++) {
      const k = seg(u, BEAT.ticks + 0.0045 * n, BEAT.ticks + 0.0045 * n + 0.25);
      g.line(x + TK.x, ty(n), x + TK.x + (LEN[n] + 1) * TK.unit, ty(n), { c: C.GREY, w: TK.w, a: 0.5, k });
    }
    // line 12 opens: its tick, a leader to its id, the id
    const xe = x + TK.x + (LEN[ROW] + 1) * TK.unit, yr = ty(ROW);
    g.line(x + TK.x, yr, xe, yr, { c: C.YELLOW, w: 0.008, a: seg(u, ts, ts + 0.25) });
    g.line(xe, yr, xl - 0.025, yr, { c: C.YELLOW, w: 0.003, a: 0.8, k: seg(u, ts, ts + 0.3) });
    T(g, ID, xl, yr, 0.052, C.YELLOW, 'left', wr(u, ts + 0.05, 0.45));
    flash(g, xl + 0.12, yr, 0.1, (u - ts) / 0.5, C.YELLOW, 10);
    // the crumb that every store holds: its id and the sha256 of its text. Each row has the name that four-run.json gives it.
    const tc = BEAT.crumb + 0.12 * j, xn = x + 0.42, xv = x + 0.62, yc = y + 0.34;
    const row = (n, label, val, c, t) => {
      T(g, label, xn, yc + 0.048 * n, 0.022, C.GREY, 'left', wr(u, t));
      T(g, val, xv, yc + 0.048 * n, 0.03, c, 'left', wr(u, t + 0.05));
    };
    row(0, 'crumb', CR.id, C.TEAL, tc);
    row(1, 'store_view', sha16(CR.sha256), C.WHITE, tc + 0.1);
    const pp = CR.per_program[key];
    if (pp.recorded !== pp.store_view) {           // hermes: what the pane printed, as it is
      row(2, 'recorded', sha16(pp.recorded), C.ORANGE, BEAT.rec);
      wrap(pp.recorded_note.split(' (')[0], 30).forEach((ln, n) => T(g, ln, xn, yc + 0.144 + n * 0.03, 0.024, C.ORANGE, 'left', wr(u, BEAT.rec + 0.12 + 0.1 * n), 0.85));
    }
    if (P.driver) wrap(P.driver, 36).forEach((ln, n) => T(g, ln, x + 0.42, y + 0.095 + n * 0.03, 0.025, C.GREY, 'left', wr(u, ts + 0.55 + 0.1 * n), 0.9));   // after its strike's burst
    // verify
    const v = RUN.verify[key], kv = wr(u, BEAT.verify + 0.12 * j), yv = y + 0.625;
    T(g, 'blobs', xl, yv, 0.026, C.GREY, 'left', kv);
    T(g, String(v.blobs), xl + 0.11, yv, 0.026, C.WHITE, 'left', kv);
    T(g, 'bad', xl + 0.23, yv, 0.026, C.GREY, 'left', kv);
    T(g, String(v.bad), xl + 0.31, yv, 0.026, C.GREEN, 'left', kv);
  }

  function codexPane(g, u, P, x, y) {
    const kk = wr(u, BEAT.codex);
    T(g, 'status', x + 0.05, y + 0.14, 0.03, C.GREY, 'left', kk);
    T(g, P.status, x + 0.2, y + 0.14, 0.03, C.RED, 'left', kk);
    wrap(P.reason, 44).forEach((ln, n) => T(g, ln, x + 0.05, y + 0.215 + n * 0.042, 0.03, C.RED, 'left', wr(u, BEAT.codex + 0.12 + 0.1 * n), 0.85));
    const kc = wr(u, BEAT.codex + 0.7);
    T(g, 'client', x + 0.05, y + 0.5, 0.026, C.GREY, 'left', kc);
    T(g, P.client.name, x + 0.19, y + 0.5, 0.026, C.WHITE, 'left', kc);
    T(g, P.client.protocol, x + 0.55, y + 0.5, 0.026, C.GREY, 'left', kc);
  }

  function pane(g, u, i) {
    const key = KEYS[i], P = RUN.programs[key], { x, y } = cellAt(i), ran = P.status === 'done', t0 = -0.8 + 0.12 * i;   // the outline draws during the flight in
    g.poly(rrectPts(x + CW / 2, y + CH / 2, CW / 2, CH / 2, 0.025), {
      c: ran ? C.GREY : C.RED, w: 0.004, a: ran ? 0.55 : 0.6, closed: true, k: seg(u, t0, t0 + 0.9), dash: ran ? null : [0.03, 0.022],
    });
    T(g, key, x + 0.04, y + 0.055, 0.034, C.WHITE, 'left', wr(u, t0 + 0.9));
    if (P.client) T(g, P.client.version, x + CW - 0.04, y + 0.055, 0.026, C.GREY, 'right', wr(u, t0 + 1.0));
    if (ran) ranPane(g, u, key, P, x, y); else codexPane(g, u, P, x, y);
  }

  // the file, the line and its id, in the middle column; the times of the two programs that ran together, under the level below
  function middle(g, u) {
    const t = BEAT.bytes;
    T(g, RUN.line_id.file, 0, -0.70, 0.026, C.GREY, 'center', wr(u, t));
    T(g, 'line', -0.025, -0.655, 0.026, C.GREY, 'right', wr(u, t));
    T(g, String(RUN.line_id.line), 0.025, -0.655, 0.026, C.WHITE, 'left', wr(u, t));
    T(g, BYTES, 0, -0.585, 0.036, C.WHITE, 'center', wr(u, t + 0.05, 0.6));
    const sa = seg(u, BEAT.idC - 0.2, BEAT.idC);
    g.line(0, -0.545, 0, -0.51, { c: C.YELLOW, w: 0.004, a: sa });
    arrowHead(g, 0, -0.495, Math.PI / 2, 0.022, C.YELLOW, sa);
    T(g, ID, 0, -0.43, 0.052, C.YELLOW, 'center', wr(u, BEAT.idC, 0.45));
    flash(g, 0, -0.43, 0.14, (u - BEAT.idC) / 0.5, C.YELLOW, 12);

    // two programs ran at the same time (bars on one clock), and one after the other (the same two bars end to end)
    const tm = RUN.times, A = tm.panes['claude-code'], B = tm.panes.opencode, bx = -0.35, sc = 0.7 / tm.overlap.sum_s, kb = seg(u, BEAT.bars, BEAT.bars + 0.9);
    T(g, 'claude-code', bx, 0.355, 0.026, C.GREY, 'left', wr(u, BEAT.bars));
    g.line(bx + A.start * sc, 0.39, bx + (A.start + A.program_s) * sc, 0.39, { c: C.TEAL, w: 0.012, a: 0.9, k: kb });
    T(g, 'opencode', bx, 0.425, 0.026, C.GREY, 'left', wr(u, BEAT.bars + 0.1));
    g.line(bx + B.start * sc, 0.46, bx + (B.start + B.program_s) * sc, 0.46, { c: C.BLUE, w: 0.012, a: 0.9, k: kb });
    const k2 = seg(u, BEAT.bars + 0.4, BEAT.bars + 1.2), la = A.program_s * sc, lb = B.program_s * sc;   // one after the other
    g.line(bx, 0.515, bx + la, 0.515, { c: C.TEAL, w: 0.012, a: 0.45, k: seg(k2, 0, 0.4) });
    g.line(bx + la, 0.515, bx + la + lb, 0.515, { c: C.BLUE, w: 0.012, a: 0.45, k: seg(k2, 0.4, 1) });
    const rows = [['together_s', tm.overlap.together_s, C.WHITE], ['wall_s', tm.overlap.wall_s, C.WHITE], ['sum_s', tm.overlap.sum_s, C.WHITE], ['lock_wait_ms', tm.lock_wait_ms, C.GREEN]];
    rows.forEach(([key, val, col], n) => {
      const kr = wr(u, BEAT.bars + 0.6 + 0.1 * n);
      T(g, key, bx, 0.58 + n * 0.042, 0.026, C.GREY, 'left', kr);
      T(g, String(val), -bx, 0.58 + n * 0.042, 0.026, col, 'right', kr);
    });
  }

  const me = F.def({
    name: 'harness', color: C.ORANGE, R: 1.65, cx: 0, cy: 0,
    zoom: 2.2, dwell: 4.8, lines: RUN.line_id.lines,
    hint: ['same bytes ⇒ same id', 2.6],
    place: () => at(0, -0.02, 0.19, 0.3, center(me.k - 1)),
    draw(g, { u }) {
      for (let i = 0; i < KEYS.length; i++) pane(g, u, i);
      middle(g, u);
    },
    tiny(g) {
      for (let i = 0; i < KEYS.length; i++) {
        const { x, y } = cellAt(i), ran = RUN.programs[KEYS[i]].status === 'done';
        g.poly(rrectPts(x + CW / 2, y + CH / 2, CW / 2, CH / 2, 0.03), { c: ran ? C.ORANGE : C.RED, w: 0.05, a: 0.8, closed: true, fill: ran ? C.ORANGE : null, fa: 0.12 });
      }
    },
    ev: [
      [-0.6, 'flute', 0, 0.3, 0], [0.7, 'flute', 2, 0.3, 0],              // the flight in: a short line that rises
      [BEAT.idC, 'bell', 0, 0.2, 0],                                      // the id in the middle
      [STRIKE['claude-code'], 'wood', 0, 0.45, -0.5],                     // line 12 opens in each store that ran: a rising run of three
      [STRIKE.opencode, 'wood', 1, 0.45, -0.5],
      [STRIKE.hermes, 'wood', 2, 0.45, 0.5],
      [BEAT.crumb, 'tick', 0, 0.2, -0.4], [BEAT.crumb + 0.12, 'tick', 1, 0.2, -0.4], [BEAT.crumb + 0.24, 'tick', 2, 0.2, 0.4],   // one crumb, one sha256, three stores
      [BEAT.verify, 'bell', 1, 0.2, 0],                                   // verify: 0 bad
      [BEAT.bars + 0.6, 'pluck', 2, 0.2, 0],
    ],
  });
})(Film);
