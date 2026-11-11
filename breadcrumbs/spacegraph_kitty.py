"""The explorer page (view.html) as a native program for Kitty and Ghostty: no Chrome, no web server.

    python3 -m breadcrumbs --home DIR spacegraph [--motion 0]

The program reads the store through the read-only View (the same API that the page calls) and draws one
picture per frame with the Kitty graphics protocol: the whole screen is one PNG (toolbar, trail, boxes,
text), so everything moves together. It needs a terminal that reads the protocol (Kitty, Ghostty).

What it shows, as view.html does (names follow the page):
- the map of sessions: boxes sized by priority, five layouts (grid, treemap, tree, radial, force), cue links;
- semantic zoom: a box shows crumb(id, level) at the level that its screen area holds (pick), so zooming in
  goes from cue words to the exact lines; a big box opens into its parts, and an open session is a document
  (a frame for each user turn, a band for each run of tools of one phase, a card for each crumb and file);
- the trail (the zoom stack): click a part to go back; Context (the Gantt of context events and the Venn);
  find in memory; follow the agent; a side panel with the text of a crumb.
- not ported: the Size panel (the ACE charts).

Keys:  arrows move the focus to the nearest box   + - zoom   shift-arrows or w a s d pan   Enter or Space show the text
       Esc or Backspace go back   1 Overview   2 Context   3 Follow the agent   4 next layout   / find   q quit
Mouse: wheel zooms about the pointer, drag pans (and throws), a click focuses a box, a click on the trail goes back.
       The program reads the mouse itself (SGR reports, CSI ? 1002/1003/1006 h, pixels with 1016 where the
       terminal has it), because xterm-kitty and xterm-ghostty have no terminfo entry here for curses.

Camera (the weight of the page's camera, Assignment C): x, y and log k are three critically damped springs
in closed form, damp(x, v, goal, omega, dt), stable at any frame time.  omega 7 for a focus move (a click on a
box, Home, a find, follow), omega 14 after a wheel step.  The wheel moves only the target and the point under
the pointer stays fixed.  A drag follows the pointer 1:1; on release the speed of the last 80 ms is kept (no glide
under 120 pixels a second, at most 4000) and the target is the camera plus the speed times 0.35 s.  A new press
stops a glide.  --motion 0 or BREADCRUMBS_MOTION=0 gives the camera of the old page (a lerp, a direct wheel, no throw).

Other settings: BREADCRUMBS_FPS (default 30), BREADCRUMBS_PIXELS (picture budget in pixels, default 1300000),
BREADCRUMBS_HOVER=0 (no pointer motion reports). A frame whose pixels did not change is not sent again.
Quit (q, Ctrl-C, SIGINT, SIGTERM, SIGHUP, a closed window, an error) leaves the alternate screen, shows the cursor,
switches the mouse modes off and deletes the image. If the window is already closed, it ends without a traceback.
"""

import base64
import json
import math
import os
import random
import re
import select
import signal
import struct
import sys
import time
import unicodedata
import zlib
from collections import deque

try:  # Windows has neither module: the pure parts of this file still import there
    import termios
    import tty
except ImportError:  # pragma: no cover
    termios = tty = None

from . import kitty

# ---- the Kitty graphics protocol and the terminal modes ----------------------------------------------------

CSI = "\x1b["
IMAGE_ID = 1


def kg_image(png, cols=0, rows=0, image_id=IMAGE_ID):
    """The escape text that shows one PNG at the cursor, cols x rows cells big (0: the terminal decides). The
    same image id and placement every time, so the next frame replaces the last one. Chunks of 4096 bytes."""
    data = base64.standard_b64encode(png).decode("ascii")
    chunks = [data[i:i + kitty.CHUNK] for i in range(0, len(data), kitty.CHUNK)] or [""]
    keys = "a=T,f=100,q=2,i=%d,p=1,C=1" % image_id
    if cols > 0:
        keys += ",c=%d" % cols
    if rows > 0:
        keys += ",r=%d" % rows
    out = []
    for n, chunk in enumerate(chunks):
        more = 1 if n < len(chunks) - 1 else 0
        out.append("\x1b_G%s;%s\x1b\\" % ((keys + ",m=%d" % more) if n == 0 else "m=%d" % more, chunk))
    return "".join(out)


def kg_frame(png, cols=0, rows=0):
    """Cursor home, then the image: each frame is drawn over the last."""
    return CSI + "H" + kg_image(png, cols, rows)


def kg_clear():
    return kitty.clear()


def enter_codes(hover=True):
    """Alternate screen, hidden cursor, mouse reports: buttons and drags (1002), every motion (1003, for the tip
    under the pointer), SGR form (1006) and pixel positions (1016, if the terminal has it), and a question
    (DECRQM) that tells whether the terminal knows 1016."""
    return ("\x1b[?1049h\x1b[?25l\x1b[?1002h" + ("\x1b[?1003h" if hover else "") + "\x1b[?1006h\x1b[?1016h"
            + "\x1b[2J\x1b[H" + "\x1b[?1016$p")


def leave_codes():
    """The way back: the image deleted, the mouse modes off, the cursor shown, the main screen again."""
    return (kg_clear() + "\x1b[?1016l\x1b[?1006l\x1b[?1003l\x1b[?1002l\x1b[?1000l\x1b[0m"
            + "\x1b[?25h\x1b[?1049l")


# ---- input: bytes from the terminal to events ------------------------------------------------------------------

SHIFT, ALT, CTRL = 1, 2, 4
CSI_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end", "Z": "shift-tab"}
TILDE_KEYS = {1: "home", 2: "insert", 3: "delete", 4: "end", 5: "pgup", 6: "pgdn", 7: "home", 8: "end"}
SGR_MOUSE = re.compile(rb"^<(\d+);(\d+);(\d+)$")


class Ev:
    """One input event. kind: key (name: up, down, left, right, home, end, pgup, pgdn, enter, esc, backspace,
    tab, delete, space, ctrl-c ...), char (text), mouse (action: press, release, move, wheel; button 0 left,
    1 middle, 2 right; x, y as the terminal gave them; dy +1 wheel up, -1 wheel down) or mode (DECRQM reply)."""

    def __init__(self, kind, name="", text="", x=0, y=0, button=0, action="", mods=0, dy=0, value=0):
        self.kind, self.name, self.text, self.x, self.y = kind, name, text, x, y
        self.button, self.action, self.mods, self.dy, self.value = button, action, mods, dy, value

    def __repr__(self):
        keys = ("kind", "name", "text", "x", "y", "button", "action", "mods", "dy", "value")
        return "Ev(%s)" % ", ".join("%s=%r" % (k, getattr(self, k)) for k in keys if getattr(self, k) not in ("", 0))

    def __eq__(self, other):
        return isinstance(other, Ev) and self.__dict__ == other.__dict__


class Input:
    """Raw bytes in, events out. It keeps a half sequence until the rest arrives; a lone ESC is the Escape key once
    flush() says that no more bytes came (the loop waits about 30 ms)."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        return self._parse(final=False)

    def flush(self):
        """No more bytes are coming: a lone ESC is a key, a half sequence is dropped."""
        return self._parse(final=True)

    def pending(self):
        return bool(self.buf)

    def _parse(self, final):
        out, b = [], self.buf
        i = 0
        while i < len(b):
            c = b[i]
            if c == 0x1b:
                if i + 1 >= len(b):
                    if final:
                        out.append(Ev("key", "esc"))
                        i += 1
                    break
                nxt = b[i + 1]
                if nxt == 0x5b:  # CSI: ESC [ params final
                    j = i + 2
                    while j < len(b) and not 0x40 <= b[j] <= 0x7e:
                        j += 1
                    if j >= len(b):
                        if final:
                            i = len(b)
                        break
                    ev = self._csi(b[i + 2:j], chr(b[j]))
                    if ev is not None:
                        out.append(ev)
                    i = j + 1
                    continue
                if nxt == 0x4f:  # SS3: ESC O final (arrows in application mode)
                    if i + 2 >= len(b):
                        if final:
                            i = len(b)
                        break
                    name = CSI_KEYS.get(chr(b[i + 2]))
                    if name:
                        out.append(Ev("key", name))
                    i += 3
                    continue
                if nxt == 0x1b:  # ESC ESC: the first one is the Escape key
                    out.append(Ev("key", "esc"))
                    i += 1
                    continue
                ch, n = self._utf8(b, i + 1)  # ESC char: Alt + char
                if ch is None:
                    if final:
                        i = len(b)
                    break
                out.append(self._char(ch, ALT))
                i += 1 + n
                continue
            ch, n = self._utf8(b, i)
            if ch is None:
                if final:
                    i = len(b)
                break
            out.append(self._char(ch, 0))
            i += n
        self.buf = b[i:]
        return out

    @staticmethod
    def _utf8(b, i):
        c = b[i]
        n = 1 if c < 0x80 else 2 if c >> 5 == 0b110 else 3 if c >> 4 == 0b1110 else 4 if c >> 3 == 0b11110 else 1
        if i + n > len(b):
            return None, 0
        return b[i:i + n].decode("utf-8", "replace"), n

    @staticmethod
    def _char(ch, mods):
        o = ord(ch)
        if ch in "\r\n":
            return Ev("key", "enter", mods=mods)
        if ch in "\x7f\x08":
            return Ev("key", "backspace", mods=mods)
        if ch == "\t":
            return Ev("key", "tab", mods=mods)
        if ch == " ":
            return Ev("key", "space", text=" ", mods=mods)
        if o < 32:
            return Ev("key", "ctrl-" + chr(o + 96), mods=mods | CTRL)
        return Ev("char", text=ch, mods=mods)

    @staticmethod
    def _csi(body, final):
        m = SGR_MOUSE.match(body)
        if m and final in "Mm":  # ESC [ < b ; x ; y M (press or motion) or m (release)
            code, x, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            mods = (SHIFT if code & 4 else 0) | (ALT if code & 8 else 0) | (CTRL if code & 16 else 0)
            if code & 64:  # the wheel: 64 up, 65 down (66 and 67 are sideways)
                return Ev("mouse", action="wheel", x=x, y=y, button=code & 3, dy=1 if code & 3 == 0 else -1 if code & 3 == 1 else 0,
                          mods=mods)
            if code & 32:  # motion: with a button held, or with none (3)
                return Ev("mouse", action="move", x=x, y=y, button=code & 3, mods=mods)
            return Ev("mouse", action="release" if final == "m" else "press", x=x, y=y, button=code & 3, mods=mods)
        if final == "y" and body.startswith(b"?") and body.endswith(b"$"):  # DECRQM reply: ESC [ ? 1016 ; 1 $ y
            parts = body[1:-1].split(b";")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                return Ev("mode", name=parts[0].decode(), value=int(parts[1]))
            return None
        params = [int(p) if p.isdigit() else 0 for p in body.decode("ascii", "replace").split(";")]
        mods = (params[1] - 1) if len(params) > 1 and params[1] > 1 else 0  # xterm: 1 + (shift 1, alt 2, ctrl 4)
        if final == "~" and params and params[0] in TILDE_KEYS:
            return Ev("key", TILDE_KEYS[params[0]], mods=mods)
        if final in CSI_KEYS and not body.startswith((b"<", b"?", b">")):
            return Ev("key", CSI_KEYS[final], mods=mods)
        return None


# ---- the picture: an RGB canvas drawn with row slices --------------------------------------------------------

BG = (11, 11, 13)
FG = (230, 230, 234)
DIM = (138, 138, 148)
FAINT = (85, 85, 95)
ACCENT = (91, 156, 255)
PANEL = (17, 17, 20)
CARD_BG = (21, 21, 25)
LINE = (36, 36, 42)
LINE2 = (52, 52, 60)
AGENT = (246, 193, 119)
KIND = {"span": (155, 140, 255), "turn": (79, 209, 197), "tool": (148, 163, 184), "sum": (246, 193, 119),
        "mem": (243, 138, 168), "text": (136, 146, 166)}
HUE = {"explore": 210, "edit": 145, "verify": 38, "delegate": 275, "result": 196, "prompt": 220, "narrative": 250,
       "turn": 220, "file": 190, "mem": 330, "sum": 45}
EVC = {"own": (108, 112, 134), "recall": (122, 162, 255), "find": (79, 209, 197), "open": (246, 193, 119),
       "compress": (230, 69, 83)}
GOOD, BAD = (63, 185, 80), (248, 81, 73)

_PX = {}


def px(c):
    """The three bytes of a colour (cached)."""
    b = _PX.get(c)
    if b is None:
        b = _PX[c] = bytes(c)
    return b


def mix(a, b, t):
    """a over b with opacity t."""
    return (int(a[0] * t + b[0] * (1 - t) + 0.5), int(a[1] * t + b[1] * (1 - t) + 0.5), int(a[2] * t + b[2] * (1 - t) + 0.5))


def hsl(h, s=0.7, l=0.64):
    """HSL (h in degrees) as an RGB tuple, as CSS hsl(h, 70%, 64%) does."""
    h = (h % 360) / 360.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h * 6) % 2 - 1))
    m = l - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][min(5, int(h * 6))]
    return (int((r + m) * 255 + 0.5), int((g + m) * 255 + 0.5), int((b + m) * 255 + 0.5))


def kind_color(kind):
    if kind in KIND:
        return KIND[kind]
    if kind in HUE:
        return hsl(HUE[kind], 0.7, 0.66)
    return ACCENT


# The glyphs: ASCII 0x20-0x7E from font8x8_basic (Daniel Hepper; after the IBM PC BIOS font; public domain),
# eight bytes per glyph, one byte per row, bit 0 is the leftmost pixel. The rest are drawn here.
FONT_HEX = (
    "0000000000000000183c3c1818001800363600000000000036367f367f363600"
    "0c3e031e301f0c00006333180c6663001c361c6e3b336e000606030000000000"
    "180c0606060c1800060c1818180c060000663cff3c660000000c0c3f0c0c0000"
    "00000000000c0c060000003f0000000000000000000c0c006030180c06030100"
    "3e63737b6f673e000c0e0c0c0c0c3f001e33301c06333f001e33301c30331e00"
    "383c36337f3078003f031f3030331e001c06031f33331e003f3330180c0c0c00"
    "1e33331e33331e001e33333e30180e00000c0c00000c0c00000c0c00000c0c06"
    "180c0603060c180000003f00003f0000060c1830180c06001e3330180c000c00"
    "3e637b7b7b031e000c1e33333f3333003f66663e66663f003c66030303663c00"
    "1f36666666361f007f46161e16467f007f46161e16060f003c66030373667c00"
    "3333333f333333001e0c0c0c0c0c1e007830303033331e006766361e36666700"
    "0f06060646667f0063777f7f6b63630063676f7b736363001c36636363361c00"
    "3f66663e06060f001e3333333b1e38003f66663e366667001e33070e38331e00"
    "3f2d0c0c0c0c1e003333333333333f0033333333331e0c006363636b7f776300"
    "6363361c1c3663003333331e0c0c1e007f6331184c667f001e06060606061e00"
    "03060c18306040001e18181818181e00081c36630000000000000000000000ff"
    "0c0c18000000000000001e303e336e000706063e66663b0000001e3303331e00"
    "3830303e33336e0000001e333f031e001c36060f06060f0000006e33333e301f"
    "0706366e666667000c000e0c0c0c1e00300030303033331e070666361e366700"
    "0e0c0c0c0c0c1e000000337f7f6b630000001f333333330000001e3333331e00"
    "00003b66663e060f00006e33333e307800003b6e66060f0000003e031e301f00"
    "080c3e0c0c2c18000000333333336e0000003333331e0c000000636b7f7f3600"
    "000063361c36630000003333333e301f00003f190c263f00380c0c070c0c3800"
    "1818180018181800070c0c380c0c07006e3b000000000000"
)
EXTRA_ART = {
    "\u25b8": ("........", ".#......", ".##.....", ".###....", ".####...", ".###....", ".##.....", ".#......"),  # ▸
    "\u22ef": ("........", "........", "........", "........", "#..#..#.", "........", "........", "........"),  # ⋯
    "\u2026": ("........", "........", "........", "........", "........", "........", "#..#..#.", "........"),  # …
    "\u00b7": ("........", "........", "........", "...##...", "...##...", "........", "........", "........"),  # ·
    "\u203a": ("........", "..#.....", "...#....", "....#...", "...#....", "..#.....", "........", "........"),  # ›
    "\u00b6": ("..#####.", ".####.#.", ".####.#.", "..###.#.", "...##.#.", "...##.#.", "...##.#.", "........"),  # ¶
    "\u2713": ("........", "......#.", ".....##.", "#...##..", "##.##...", ".###....", "..#.....", "........"),  # ✓
    "\u270e": (".....##.", "....####", "...###..", "..###...", ".###....", ".##.....", "##......", "........"),  # ✎
    "\u2315": (".####...", "#....#..", "#....#..", "#....#..", ".####...", "....##..", ".....##.", "......#."),  # ⌕
    "\u21c4": ("....#...", ".....#..", "#######.", ".....#..", "....#...", "...#....", "#######.", "...#...."),  # ⇄
    "\u25a4": (".######.", ".#....#.", ".######.", ".#....#.", ".######.", ".#....#.", ".######.", "........"),  # ▤
    "\u25c6": ("...##...", "..####..", ".######.", "########", ".######.", "..####..", "...##...", "........"),  # ◆
    "\u03a3": (".######.", ".#......", "..#.....", "...#....", "..#.....", ".#......", ".######.", "........"),  # Σ
    "\u25cf": ("........", "..####..", ".######.", ".######.", ".######.", ".######.", "..####..", "........"),  # ●
    "\u2022": ("........", "........", "..###...", ".#####..", "..###...", "........", "........", "........"),  # •
    "\u00d7": ("........", ".#...#..", "..#.#...", "...#....", "..#.#...", ".#...#..", "........", "........"),  # ×
    "\u2192": ("........", "....#...", ".....#..", "#######.", ".....#..", "....#...", "........", "........"),  # →
    "\u2190": ("........", "...#....", "..#.....", ".#######", "..#.....", "...#....", "........", "........"),  # ←
    "\u2191": ("...#....", "..###...", ".#.#.#..", "...#....", "...#....", "...#....", "...#....", "........"),  # ↑
}
FOLD = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-", "\u00a0": " ",
        "\t": "  ", "\u2265": ">=", "\u2264": "<=", "\u2212": "-"}
GLYPHS = {}


def _load_font():
    blob = "".join(FONT_HEX)
    for i in range(95):
        GLYPHS[chr(0x20 + i)] = tuple(int(blob[i * 16 + j * 2:i * 16 + j * 2 + 2], 16) for j in range(8))
    for ch, art in EXTRA_ART.items():
        if len(ch) == 1:
            GLYPHS[ch] = tuple(sum(1 << x for x, c in enumerate(row) if c == "#") for row in art)


_load_font()
UNKNOWN = GLYPHS["?"]


def plain(s):
    """Text that the font can draw: folds quotes and dashes, strips accents, and shows '?' for the rest (at draw time)."""
    if all(ch in GLYPHS or ch == "\n" for ch in s):
        return s
    out = []
    for ch in s:
        if ch in GLYPHS:
            out.append(ch)
        elif ch in FOLD:
            out.append(FOLD[ch])
        elif ch == "\n":
            out.append(ch)
        elif ch in "\r\x0b\x0c":
            continue
        else:
            d = unicodedata.normalize("NFKD", ch)
            d = "".join(x for x in d if x in GLYPHS and not unicodedata.combining(x))
            out.append(d or "?")
    return "".join(out)


_ROWTAB = {}
_LINES = {}


def _row_table(fg, bg, scale):
    key = (fg, bg, scale)
    t = _ROWTAB.get(key)
    if t is None:
        on, off = px(fg) * scale, px(bg) * scale
        t = _ROWTAB[key] = [b"".join(on if bits >> x & 1 else off for x in range(8)) for bits in range(256)]
    return t


def line_rows(s, fg, bg, scale=1):
    """The pixel rows (8 x scale of them, RGB bytes) of one line of text on a flat background."""
    key = (s, fg, bg, scale)
    rows = _LINES.get(key)
    if rows is None:
        tab = _row_table(fg, bg, scale)
        gl = [GLYPHS.get(ch, UNKNOWN) for ch in s]
        rows = [r for r in (b"".join([tab[g[i]] for g in gl]) for i in range(8)) for _ in range(scale)]
        if len(_LINES) > 6000:
            _LINES.clear()
        _LINES[key] = rows
    return rows


class Canvas:
    """An RGB picture. The buffer holds the PNG's raw data (a filter byte 0 in front of each row), so a frame
    goes to zlib as it is. Every shape clips to self.clip. Colours are (r, g, b) tuples."""

    def __init__(self, w, h, bg=BG):
        self.w, self.h, self.bg = int(w), int(h), tuple(bg)
        self.stride = 3 * self.w + 1
        self.buf = bytearray(self.stride * self.h)
        self.clip = (0, 0, self.w, self.h)
        self.clear()

    def clear(self, color=None):
        c = tuple(color or self.bg)
        self.buf[:] = (b"\x00" + px(c) * self.w) * self.h
        self.clip = (0, 0, self.w, self.h)

    def set_clip(self, x0, y0, x1, y1):
        """Clip to a rectangle (inside the picture); returns the old one."""
        old = self.clip
        self.clip = (max(0, min(self.w, round(x0))), max(0, min(self.h, round(y0))),
                     max(0, min(self.w, round(x1))), max(0, min(self.h, round(y1))))
        return old

    def rect(self, x, y, w, h, c):
        cx0, cy0, cx1, cy1 = self.clip
        x0, y0, x1, y1 = round(max(cx0, x)), round(max(cy0, y)), round(min(cx1, x + w)), round(min(cy1, y + h))
        if x1 <= x0 or y1 <= y0:
            return
        run = px(c) * (x1 - x0)
        n, s, buf = len(run), self.stride, self.buf
        o = y0 * s + 1 + 3 * x0
        for _ in range(y1 - y0):
            buf[o:o + n] = run
            o += s

    def at(self, x, y):
        """The colour at a pixel (for tests)."""
        o = y * self.stride + 1 + 3 * x
        return tuple(self.buf[o:o + 3])

    def rrect(self, x, y, w, h, r, c):
        """A filled rectangle with round corners (radius r)."""
        x, y, w, h = round(x), round(y), round(w), round(h)
        if w <= 0 or h <= 0:
            return
        r = max(0, min(int(r), w // 2, h // 2))
        if r == 0:
            return self.rect(x, y, w, h, c)
        for i, inset in enumerate(_corner(r)):
            self.rect(x + inset, y + i, w - 2 * inset, 1, c)
            self.rect(x + inset, y + h - 1 - i, w - 2 * inset, 1, c)
        self.rect(x, y + r, w, h - 2 * r, c)

    def frame(self, x, y, w, h, r, fill, border, bw=1):
        """A round box with a border of width bw."""
        self.rrect(x, y, w, h, r, border)
        self.rrect(x + bw, y + bw, w - 2 * bw, h - 2 * bw, max(0, r - bw), fill)

    def disc(self, cx, cy, r, c):
        r = max(0.5, r)
        for dy in range(-int(r), int(r) + 1):
            half = math.sqrt(max(0.0, r * r - dy * dy))
            self.rect(round(cx - half), round(cy) + dy, round(2 * half) + 1, 1, c)

    def line(self, x0, y0, x1, y1, c, w=1):
        """A line (steps of one pixel, clipped first). w > 1 draws w x w blocks."""
        seg = clip_line(x0, y0, x1, y1, *self.clip)
        if seg is None:
            return
        x0, y0, x1, y1 = seg
        dx, dy = x1 - x0, y1 - y0
        n = int(max(abs(dx), abs(dy)))
        if w == 1 and abs(dy) < 0.5:
            return self.rect(min(x0, x1), round(y0), abs(dx) + 1, 1, c)
        if w == 1 and abs(dx) < 0.5:
            return self.rect(round(x0), min(y0, y1), 1, abs(dy) + 1, c)
        if n == 0:
            return self.rect(round(x0) - w // 2, round(y0) - w // 2, w, w, c)
        sx, sy, buf, s, p = dx / n, dy / n, self.buf, self.stride, px(c)
        cx0, cy0, cx1, cy1 = self.clip
        for i in range(n + 1):
            xi, yi = round(x0 + i * sx), round(y0 + i * sy)
            if w == 1:
                if cx0 <= xi < cx1 and cy0 <= yi < cy1:
                    o = yi * s + 1 + 3 * xi
                    buf[o:o + 3] = p
            else:
                self.rect(xi - w // 2, yi - w // 2, w, w, c)

    def dashed(self, x0, y0, x1, y1, c, on=6, off=10, phase=0.0, w=1):
        """A dashed line: on pixels drawn, off pixels left, starting `phase` pixels into the pattern."""
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1 or length > 20000:
            return
        ux, uy, period = (x1 - x0) / length, (y1 - y0) / length, on + off
        s = -(phase % period)
        while s < length:
            a, b = max(0.0, s), min(length, s + on)
            if b > a:
                self.line(x0 + ux * a, y0 + uy * a, x0 + ux * b, y0 + uy * b, c, w)
            s += period

    def text(self, x, y, s, fg, bg, scale=1):
        """One line of text, top left at (x, y), on a flat background of colour bg (the glyph cell is painted)."""
        if not s:
            return
        x, y = round(x), round(y)
        cx0, cy0, cx1, cy1 = self.clip
        width = 8 * scale * len(s)
        x0, x1 = max(cx0, x), min(cx1, x + width)
        if x1 <= x0 or y >= cy1 or y + 8 * scale <= cy0:
            return
        rows, buf, st = line_rows(s, fg, bg, scale), self.buf, self.stride
        a, b = 3 * (x0 - x), 3 * (x1 - x)
        n = b - a
        for i, row in enumerate(rows):
            yy = y + i
            if cy0 <= yy < cy1:
                o = yy * st + 1 + 3 * x0
                buf[o:o + n] = row[a:b]

    def png(self, level=1):
        return make_png(self.w, self.h, self.buf, level)


_CORNERS = {}


def _corner(r):
    """For a corner of radius r: how far each of the first r rows starts inside the box."""
    t = _CORNERS.get(r)
    if t is None:
        t = _CORNERS[r] = [r - int(math.sqrt(max(0.0, r * r - (r - i - 0.5) ** 2)) + 0.5) for i in range(r)]
    return t


def clip_line(x0, y0, x1, y1, cx0, cy0, cx1, cy1):
    """Liang-Barsky: the part of a line inside [cx0, cx1) x [cy0, cy1), or None."""
    if not all(map(math.isfinite, (x0, y0, x1, y1))):
        return None
    cx1, cy1 = cx1 - 1, cy1 - 1
    dx, dy, t0, t1 = x1 - x0, y1 - y0, 0.0, 1.0
    for p, q in ((-dx, x0 - cx0), (dx, cx1 - x0), (-dy, y0 - cy0), (dy, cy1 - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy


def make_png(w, h, raw, level=1):
    """An 8-bit RGB PNG from raw rows that already start with a filter byte."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, level)) + chunk(b"IEND", b""))


def geometry(cols, rows, xpix, ypix, budget=1300000):
    """The picture for a window: (width, height, shrink, unit). The window is xpix x ypix pixels (a guess of 9 x 18
    a cell when the terminal says 0). The picture is that size divided by a shrink of at least 1 that keeps it under
    the pixel budget (a Retina window of 2880 x 1800 gives 2.0). unit is the glyph scale: 1 for a cell about 16
    picture pixels high, 2 for about 32."""
    if xpix <= 0 or ypix <= 0:
        xpix, ypix = cols * 9, rows * 18
    shrink = max(1.0, math.ceil(math.sqrt(xpix * ypix / float(budget)) * 20 - 1e-9) / 20.0)
    w, h = max(160, int(xpix / shrink)), max(96, int(ypix / shrink))
    return w, h, shrink, max(1, int(round(h / max(1, rows) / 16.0)))


# ---- the camera: critically damped springs ---------------------------------------------------------------------

OMEGA_FOCUS = 7.0    # a focus move: a click on a box, Home, a find, follow the agent; and a throw after a drag
OMEGA_WHEEL = 14.0   # after a wheel step
THROW_SECONDS = 0.35  # the target of a throw: camera + hand speed * this
THROW_WINDOW = 0.08  # the hand speed is the speed over the last 80 ms
WHEEL_STEP = 0.16    # log k for one wheel step
K_MIN, K_MAX = 0.02, 1e6
FLICK = 120.0        # the hand speed (pixels a second) under which a release gives no glide (the page: MOVE.flick)
SPEED_MAX = 4000.0   # the fastest throw, pixels a second (the page: MOVE.fast)


def damp(x, v, goal, omega, dt):
    """One step of a critically damped spring, in closed form: (x, v) after dt seconds. It is exact, so it is stable
    at any dt, and many small steps give the same result as one big one. From rest it never passes the goal."""
    d = x - goal
    e = math.exp(-omega * dt)
    c = v + omega * d
    return goal + (d + c * dt) * e, (v - omega * c * dt) * e


def keep_inside(x, v, goal, omega):
    """The speed that a spring may keep when its goal changes: if the speed would carry x past the goal, the
    spring has no overshoot left to give, so the speed becomes the one that just arrives (no bounce)."""
    d = x - goal
    return -omega * d if (v + omega * d) * d < 0 else v


class Camera:
    """x, y and k (the zoom, as a log) chase a target with springs. The wheel moves the target only, and the world point under the pointer stays
    under it. A drag is direct; a throw keeps the hand's speed. motion=False is the camera of the old page."""

    def __init__(self, motion=True, k=0.5):
        self.motion = motion
        self.x = self.y = self.tx = self.ty = 0.0
        self.k = self.tk = k
        self.vx = self.vy = self.vk = 0.0
        self.omega = OMEGA_FOCUS
        self.anchor = None   # (world x, world y, pointer x, pointer y) while a wheel zoom runs
        self.gliding = False

    def snap(self):
        self.x, self.y, self.k = self.tx, self.ty, self.tk
        self.vx = self.vy = self.vk = 0.0
        self.anchor, self.gliding = None, False

    def moving(self):
        k = self.k
        return bool(self.anchor or self.vx or self.vy or self.vk or abs(self.x - self.tx) * k > 0.02
                    or abs(self.y - self.ty) * k > 0.02 or abs(math.log(self.k / self.tk)) > 1e-4)

    def aim(self, x, y, k, omega=OMEGA_FOCUS):
        """A focus move: the target is a world point and a zoom. Any glide ends."""
        self.tx, self.ty, self.tk = x, y, max(K_MIN, min(K_MAX, k))
        self.omega = omega
        self.gliding = False
        if self.motion:
            self.vx = keep_inside(self.x, self.vx, self.tx, omega)
            self.vy = keep_inside(self.y, self.vy, self.ty, omega)
            self.vk = keep_inside(math.log(self.k), self.vk, math.log(self.tk), omega)
            self.anchor = None

    def grab(self):
        """A press stops everything: the camera stays where it is."""
        self.tx, self.ty, self.tk = self.x, self.y, self.k
        self.vx = self.vy = self.vk = 0.0
        self.anchor, self.gliding = None, False

    def stop_glide(self):
        if self.gliding:
            self.tx, self.ty = self.x, self.y
            self.vx = self.vy = 0.0
            self.gliding = False

    def drag(self, x, y):
        """The camera follows the pointer 1:1 (no lag)."""
        self.x = self.tx = x
        self.y = self.ty = y
        self.vx = self.vy = 0.0
        self.anchor = None

    def throw(self, vx, vy):
        """On release: the hand's speed in world units a second. The target is camera + speed * 0.35 s; the spring carries on."""
        self.anchor = None
        self.omega = OMEGA_FOCUS
        self.vx, self.vy = vx, vy
        self.tx, self.ty = self.x + vx * THROW_SECONDS, self.y + vy * THROW_SECONDS
        self.gliding = True

    def zoom_at(self, px, py, steps, mid_x, mid_y):
        """A wheel step (steps may be fractional): the new zoom comes from the target zoom, so quick steps add up."""
        wx, wy = (px - mid_x) / self.k + self.x, (py - mid_y) / self.k + self.y   # the world point under the pointer
        tk = max(K_MIN, min(K_MAX, self.tk * math.exp(steps * WHEEL_STEP)))
        self.stop_glide()
        if not self.motion:  # the old page: a jump
            self.k = self.tk = tk
            self.x = self.tx = wx - (px - mid_x) / tk
            self.y = self.ty = wy - (py - mid_y) / tk
            return
        self.tk, self.omega = tk, OMEGA_WHEEL
        self.tx, self.ty = wx - (px - mid_x) / tk, wy - (py - mid_y) / tk
        self.vx = self.vy = 0.0
        self.anchor = (wx, wy, px, py)
        self.vk = keep_inside(math.log(self.k), self.vk, math.log(self.tk), self.omega)

    def pan_by(self, dx, dy, omega=OMEGA_WHEEL):
        """Move the target by a world step (the keys): the spring eases the camera there."""
        self.stop_glide()
        self.tx += dx
        self.ty += dy
        if not self.motion:
            return
        self.omega, self.anchor = omega, None
        self.vx = keep_inside(self.x, self.vx, self.tx, omega)
        self.vy = keep_inside(self.y, self.vy, self.ty, omega)

    def step(self, dt, mid_x, mid_y):
        if dt <= 0:
            return
        if not self.motion:  # the old page: a share of the way each frame, x, y and log k
            q = 1 - math.exp(-dt * 6)
            self.x += (self.tx - self.x) * q
            self.y += (self.ty - self.y) * q
            self.k = math.exp(math.log(self.k) + (math.log(self.tk) - math.log(self.k)) * q)
            if abs(self.x - self.tx) * self.k < 0.02 and abs(self.y - self.ty) * self.k < 0.02 and abs(math.log(self.k / self.tk)) < 1e-4:
                self.x, self.y, self.k = self.tx, self.ty, self.tk
            return
        w = self.omega
        lk, self.vk = damp(math.log(self.k), self.vk, math.log(self.tk), w, dt)
        self.k = max(K_MIN, min(K_MAX, math.exp(lk)))
        if self.anchor:  # the point under the pointer stays put while the zoom runs
            wx, wy, px, py = self.anchor
            self.x, self.y = wx - (px - mid_x) / self.k, wy - (py - mid_y) / self.k
            if abs(math.log(self.k / self.tk)) < 2e-4 and abs(self.vk) < 2e-3:
                self.k, self.vk, self.anchor = self.tk, 0.0, None
                self.x, self.y = self.tx, self.ty
            return
        self.x, self.vx = damp(self.x, self.vx, self.tx, w, dt)
        self.y, self.vy = damp(self.y, self.vy, self.ty, w, dt)
        k = self.k  # arrived: snap, so a frame at rest is the same frame
        if abs(self.x - self.tx) * k < 0.02 and abs(self.vx) * k < 0.2:
            self.x, self.vx = self.tx, 0.0
        if abs(self.y - self.ty) * k < 0.02 and abs(self.vy) * k < 0.2:
            self.y, self.vy = self.ty, 0.0
        if abs(math.log(k / self.tk)) < 1e-4 and abs(self.vk) < 1e-3:
            self.k, self.vk = self.tk, 0.0
        if not (self.vx or self.vy):
            self.gliding = False


class Drag:
    """The pointer samples of one drag, to find the hand's speed at the release."""

    def __init__(self, x, y, cam_x, cam_y, t):
        self.x, self.y, self.cx, self.cy, self.moved = x, y, cam_x, cam_y, False
        self.samples = [(t, x, y)]

    def add(self, t, x, y):
        self.samples.append((t, x, y))
        while len(self.samples) > 2 and t - self.samples[1][0] > 4 * THROW_WINDOW:
            self.samples.pop(0)

    def speed(self, t):
        """Pixels a second over the last 80 ms of the drag. (0, 0) if the hand had stopped 40 ms or more before t, or if
        the last 80 ms hold too few samples (the page does the same)."""
        last = self.samples[-1]
        if t - last[0] > THROW_WINDOW / 2:
            return 0.0, 0.0
        live = [s for s in self.samples if last[0] - s[0] <= THROW_WINDOW]
        if len(live) < 2 or last[0] - live[0][0] < 0.008:
            return 0.0, 0.0
        dt = last[0] - live[0][0]
        return (last[1] - live[0][1]) / dt, (last[2] - live[0][2]) / dt


# ---- the layouts (the page's Force2D and its fixed layouts) -----------------------------------------------------

REPEL, GRAVITY, SPRING, GAP, LINK, PAD = 6.0, 0.003, 0.02, 26.0, 40.0, 22.0
LEVELS = [0] + [2 ** (i - 12) for i in range(12)] + [1]
CARD, FILE_H, GX, GY, COLS, FPAD, FHEAD, BHEAD, BPAD, FGAP = (220, 72), 56, 14, 12, 4, 16, 30, 24, 8, 48
ROWW = COLS * (CARD[0] + 2 * BPAD) + (COLS - 1) * GX
FRAME = ROWW + 2 * FPAD
LAYOUTS = (("grid", "Grid"), ("treemap", "Treemap"), ("tree", "Tree"), ("radial", "Radial"), ("force", "Force"))


class Node:
    """A crumb at one place. key: its id for a session, parent key + ">" + slot for a part. x, y, w, h is the box in
    the world; tx, ty, tw, th the place that a fixed layout gives it. Cards of a session document carry in_doc."""

    def __init__(self, key, info=None, parent=None):
        self.key, self.parent = key, parent
        self.id, self.kind, self.title, self.cues, self.session = key, "text", "", [], ""
        self.tokens, self.heat, self.pri = 0, 0.0, 0.5
        self.kids = None
        self.x = self.y = self.vx = self.vy = 0.0
        self.s, self.w, self.h = 40.0, 0.0, 0.0
        self.tx = self.ty = self.tw = self.th = 0.0
        self.fx = self.fy = self.r = 0.0
        self.texts, self.asked, self.glow = {}, set(), 0.0
        self.sub, self.tup, self.lx, self.ly, self.ang, self.span, self.rings, self.many = [], None, 0.0, 0.0, 0.0, 1, [], 1
        self.in_doc = self.sess = self.doc = None
        self.doc_ready = self.loading = self.shown = False
        self.mono = self.synthetic = self.full_asked = False   # the cards of a session document
        self.label = self.detail = self.head = self.say = self.tip = self.trail = self.st = self.sum = self.path = ""
        self.gist = self.full = None
        self.count = 0
        self.__dict__.update(info or {})


class Edge:
    def __init__(self, source, target, w=1.0, shared=(), kind="cue"):
        self.source, self.target, self.w, self.shared, self.kind = source, target, w, list(shared), kind
        self.k, self.tree = 1.0, False


def force_step(roots, edges, temp, rng):
    """One step of the force layout: all pairs repel, cue links pull like springs, a weak pull to the centre, and boxes
    never overlap. Returns the new temperature (the page stops below 0.004)."""
    if temp < 0.004 or not roots:
        return temp
    by, n = {a.key: a for a in roots}, len(roots)
    for a in roots:
        a.fx, a.fy, a.r = -a.x * GRAVITY, -a.y * GRAVITY, a.s * 0.72
    for i in range(n):
        A = roots[i]
        for j in range(i + 1, n):
            B = roots[j]
            dx, dy = B.x - A.x, B.y - A.y
            d2 = dx * dx + dy * dy
            if d2 < 1e-6:
                dx, dy = rng.random() - 0.5, rng.random() - 0.5
                d2 = max(1e-12, dx * dx + dy * dy)
            d = math.sqrt(d2)
            f = REPEL * A.r * B.r / d2 / d
            A.fx -= dx * f
            A.fy -= dy * f
            B.fx += dx * f
            B.fy += dy * f
    for e in edges:
        A, B = by.get(e.source), by.get(e.target)
        if not A or not B:
            continue
        dx, dy = B.x - A.x, B.y - A.y
        d = math.hypot(dx, dy) or 1e-3
        f = SPRING * e.k * (d - A.r - B.r - LINK) / d
        A.fx += dx * f
        A.fy += dy * f
        B.fx -= dx * f
        B.fy -= dy * f
    for a in roots:
        a.vx, a.vy = (a.vx + a.fx * temp) * 0.7, (a.vy + a.fy * temp) * 0.7
        a.x += max(-30.0, min(30.0, a.vx))
        a.y += max(-30.0, min(30.0, a.vy))
    for i in range(n):
        A = roots[i]
        for j in range(i + 1, n):
            B = roots[j]
            lim, dx, dy = A.r + B.r + GAP, B.x - A.x, B.y - A.y
            d = math.hypot(dx, dy)
            if d >= lim or d < 1e-6:
                continue
            push = (lim - d) / 2 / d
            A.x -= dx * push
            A.y -= dy * push
            B.x += dx * push
            B.y += dy * push
    return temp * 0.992


def pack(blocks, aspect):
    """Blocks in rows, the rows about as wide as the window is for its height."""
    area = sum((b["w"] + PAD) * (b["h"] + PAD) for b in blocks)
    wide = max([math.sqrt(area * aspect)] + [b["w"] for b in blocks])
    x = y = rh = 0.0
    for b in blocks:
        if x > 0 and x + b["w"] > wide:
            x, y, rh = 0.0, y + rh + 2 * PAD, 0.0   # room for the labels under a row
        b["at"](x, y)
        x += b["w"] + PAD
        rh = max(rh, b["h"])


def block(t, c, ring=0.0):
    """A tree (each box at lx, ly round its root at 0, 0) as one block for pack(); ring: its outer ring."""
    all_, stack = [], [t]
    while stack:
        n = stack.pop()
        all_.append(n)
        stack.extend(reversed(n.sub))
    xs, ys = [n.lx for n in all_], [n.ly for n in all_]
    x0, y0 = min([-ring] + xs) - c / 2, min([-ring] + ys) - c / 2

    def at(x, y):
        for n in all_:
            n.tx, n.ty = x + n.lx - x0, y + n.ly - y0
    return {"w": max([ring] + xs) + c / 2 - x0, "h": max([ring] + ys) + c / 2 - y0, "at": at}


def squarify(cells, x, y, w, h):
    """The squarified treemap (Bruls, Huizing and van Wijk 2000): rows along the short side; a row takes the next cell
    while that makes its worst cell more square. cells: dicts with v (the area), which get x, y, w, h."""
    i = 0
    while i < len(cells):
        side = max(1e-9, min(w, h))
        first = cells[i]["v"]

        def worst(j, total, side=side, first=first):
            t = max(1e-12, (total / side) ** 2)
            return max(t / cells[j - 1]["v"], first / t)
        j, total = i + 1, first
        while j < len(cells) and worst(j + 1, total + cells[j]["v"]) <= worst(j, total):
            total += cells[j]["v"]
            j += 1
        t = total / side
        o = 0.0
        while i < j:
            ln = cells[i]["v"] / t
            cells[i].update({"x": x, "y": y + o, "w": t, "h": ln} if w >= h else {"x": x + o, "y": y, "w": ln, "h": t})
            o += ln
            i += 1
        if w >= h:
            x, w = x + t, w - t
        else:
            y, h = y + t, h - t


def forest(roots, edges):
    """A tree for each group of linked sessions, the biggest first. The root is the hub (the session with the fewest
    links to the farthest one); a session hangs from its strongest link one step nearer the hub (a tree link)."""
    by = {a.key: a for a in roots}
    adj = {a.key: [] for a in roots}
    for e in edges:
        if e.source in adj and e.target in adj:
            adj[e.source].append((e.target, e))
            adj[e.target].append((e.source, e))

    def hops(k):
        d, q = {k: 0}, [k]
        for p in q:
            for m, _ in adj[p]:
                if m not in d:
                    d[m] = d[p] + 1
                    q.append(m)
        return d
    far, trees, seen = {}, [], set()
    for a in roots:
        if a.key in seen:
            continue
        group = list(hops(a.key))
        for k in group:
            far[k] = max(hops(k).values()) - len(adj[k]) / 1e3
        hub = group[0]
        for k in group[1:]:
            if far[k] < far[hub]:
                hub = k
        d = hops(hub)
        for k in group:
            seen.add(k)
            by[k].tup, by[k].sub = None, []
        for n in roots:
            if d.get(n.key, -1) > 0:   # in the order of the sessions, so the children of a session are too
                up = None
                for m, e in adj[n.key]:
                    if d.get(m) == d[n.key] - 1 and (up is None or e.w > up[1].w):
                        up = (m, e)
                up[1].tree = True
                n.tup = by[up[0]]
                n.tup.sub.append(n)
        by[hub].many = len(group)
        trees.append(by[hub])
    trees.sort(key=lambda t: -t.many)
    return trees


def tree_at(t, c):
    """Top down: each leaf takes the next column, and a parent sits over the middle of its children."""
    col = [0]

    def put(n, depth):
        n.ly = depth * (c + 3 * PAD)
        if not n.sub:
            n.lx = col[0] * (c + PAD)
            col[0] += 1
            return
        for k in n.sub:
            put(k, depth + 1)
        n.lx = (n.sub[0].lx + n.sub[-1].lx) / 2
    put(t, 0)
    return block(t, c)


def rings_at(t, c):
    """The hub in the middle and each depth on a ring; a session's angle is the middle of its leaves'."""
    by = []

    def leaves(n):
        n.span = sum(leaves(k) for k in n.sub) if n.sub else 1
        return n.span
    leaves(t)

    def turn(n, a, depth):  # a: where the part of the circle for n's leaves starts
        n.ang = a + math.pi * n.span / t.span
        while len(by) <= depth:
            by.append([])
        by[depth].append(n)
        for k in n.sub:
            turn(k, a, depth + 1)
            a += 2 * math.pi * k.span / t.span
    turn(t, -math.pi / 2, 0)
    d = math.sqrt(2) * (c + PAD / 2)

    def chord(da):  # squares d apart never touch
        return d / 2 / max(1e-6, math.sin(min(math.pi, da) / 2))
    r = 0.0
    t.rings = []
    for i, ring in enumerate(by):
        if i:
            far = [(chord(n.ang - ring[j - 1].ang) if j else chord(2 * math.pi - ring[-1].ang + n.ang) if len(ring) > 1 else 0.0)
                   for j, n in enumerate(ring)]
            r = max([r + d] + far)
            t.rings.append(r)
        for n in ring:
            n.lx, n.ly = r * math.cos(n.ang), r * math.sin(n.ang)
    return block(t, c, r)


def arrange(roots, edges, layout, aspect):
    """The place of each box (tx, ty) and, in the treemap, its size (tw, th), round 0, 0. Force has no fixed place."""
    for a in roots:
        a.tw = a.th = 0.0
    for e in edges:
        e.tree = False
    if layout == "force" or not roots:
        return
    c = max(a.s for a in roots)
    if layout == "grid":
        blocks = []
        for a in roots:
            a.lx = a.ly = 0.0
            a.sub = []
            blocks.append(block(a, c))
        pack(blocks, aspect)
    elif layout == "treemap":  # a cell grows with the session's size (the root of its tokens) and is never smaller than its grid cell
        def wt(a):
            return math.sqrt(a.tokens or 0) + 100
        kk = max((a.s + PAD) ** 2 / wt(a) for a in roots)
        cells = sorted(({"a": a, "v": kk * wt(a)} for a in roots), key=lambda q: -q["v"])
        area = sum(q["v"] for q in cells)
        squarify(cells, 0.0, 0.0, math.sqrt(area * aspect), math.sqrt(area / aspect))
        for q in cells:
            a = q["a"]
            a.tx, a.ty, a.tw, a.th = q["x"] + q["w"] / 2, q["y"] + q["h"] / 2, q["w"] - PAD, q["h"] - PAD
    else:
        pack([(tree_at if layout == "tree" else rings_at)(t, c) for t in forest(roots, edges)], aspect)
    x0, x1 = min(a.tx for a in roots), max(a.tx for a in roots)
    y0, y1 = min(a.ty for a in roots), max(a.ty for a in roots)
    for a in roots:
        a.tx -= (x0 + x1) / 2
        a.ty -= (y0 + y1) / 2


def place(roots, layout, q):
    """Each box a step (q = 1: all the way) to its place and size; the force model moves it in its own way."""
    fixed = layout != "force"
    for a in roots:
        if fixed:
            a.x += (a.tx - a.x) * q
            a.y += (a.ty - a.y) * q
        a.w += (((fixed and a.tw) or a.s) - a.w) * q
        a.h += (((fixed and a.th) or a.s) - a.h) * q


def layout_kids(p, nodes):
    """The parts of a box sit in a grid in its lower 60%."""
    pw, ph, m = p.w or p.s, p.h or p.s, len(p.kids)
    bw, bh, top = pw * 0.94, ph * 0.6, p.y - ph / 2 + ph * 0.37
    cols = max(1, math.ceil(math.sqrt(m * bw / bh)))
    cell = min(bw / cols, bh / math.ceil(m / cols))
    left = p.x - min(cols, m) * cell / 2
    top1 = max([1] + [nodes[key].tokens or 1 for key in p.kids])
    for i, key in enumerate(p.kids):
        k = nodes[key]
        rel = (1 + math.log1p(k.tokens or 1)) / (1 + math.log1p(top1))
        k.x, k.y = left + (i % cols + 0.5) * cell, top + (i // cols + 0.5) * cell
        k.s = cell * (0.5 + 0.42 * rel)
        if k.kids:
            layout_kids(k, nodes)


# ---- semantic zoom and the text of crumbs ----------------------------------------------------------------------

def pick(tokens, chars):
    """The level of crumb(id, level) that a box can hold: the biggest level whose text (about 4 characters a token) fits."""
    level = 0
    for lv in LEVELS:
        if lv * (tokens or 1) * 4 <= chars * 1.5:
            level = lv
    return level


def handle(n):
    return "\u25b8 %s %s \u00b7%dt" % (n.id, n.kind, n.tokens) + (" | " + " ".join(n.cues) if n.cues else "")


def name(n):
    if n.in_doc:
        return n.trail
    return n.kind + " " + (" ".join(n.cues[:3]) if n.cues else (n.title or n.id)[:26])


def wrap(line, cols):
    """Whole words to the box width; a handle line or a longer word is cut later."""
    if re.match(r"^\s*(\u25b8|\u22ef) ", line):
        return [line]
    cols = max(1, cols)
    out = []
    while len(line) > cols:
        cut = line.rfind(" ", 0, cols + 1)
        if cut < cols / 3:
            cut = cols
        out.append(line[:cut])
        line = line[cut:].lstrip(" ")
    return out + [line]


_SPLIT = re.compile(r'([^\n])(?=\u25b8 \S+ \w+(?: "[^"\n]*")? \u00b7\d+t)')
_HANDLE = re.compile(r'^(\s*)\u25b8 \S+ (\w+)(?: "(.*?)")? \u00b7\d+t(.*)$')
_GAPLINE = re.compile(r"^(\s*)\u22ef .*$")


def tidy(s, own=False):
    """A crumb's text as words: its own handle line goes, the handles of its parts keep their kind and title, and a gap is
    an ellipsis. Ids and token counts are for machines."""
    s = _SPLIT.sub(lambda m: m.group(1) + "\n", s or "")
    out = []
    for i, line in enumerate(s.split("\n")):
        if i == 0 and own and line.startswith("\u25b8 "):
            continue
        line = _HANDLE.sub(lambda m: (m.group(1) + "\u25b8 " + m.group(2) + " " + (m.group(3) or "") + m.group(4)).rstrip(), line)
        out.append(_GAPLINE.sub(lambda m: m.group(1) + "\u2026", line))
    return "\n".join(out).strip()


def flat(s):
    """One line for a bar or a card: its text lines, or else the titles of the parts that the model saw as handles."""
    ls = [x.strip() for x in tidy(s, True).split("\n")]
    ls = [x for x in ls if x and x != "\u2026"]
    txt = [x for x in ls if not x.startswith("\u25b8 ")]
    if txt:
        return " ".join(txt)
    return " \u00b7 ".join(x for x in (re.sub(r"^\u25b8 \w+\s*", "", x) for x in ls) if x)


PHASE_OF = {}
for _ph, _names in (
        ("explore", "read_file search_files web_search web_extract skill_view skills_list session_search tool_search "
                    "tool_describe vision_analyze browser_exec browser_navigate browser_snapshot browser_vision crumb crumb_find"),
        ("edit", "write_file patch skill_manage memory context_notes todo todo_list"),
        ("verify", "terminal execute_code process process_manage"),
        ("delegate", "delegate_task message_agent send_message clarify")):
    for _t in _names.split():
        PHASE_OF[_t] = _ph
GLYPH = {"explore": "\u2315", "edit": "\u270e", "verify": "$", "delegate": "\u21c4", "result": "\u2713", "prompt": "\u25b8",
         "narrative": "\u00b6", "file": "\u25a4", "mem": "\u25c6", "sum": "\u03a3"}


def bare_tool(v):
    t = str(v or "tool").strip() or "tool"
    return t[5:] if t.startswith("mcp__") else t


def first_str(v):
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple)):
        return next((s for s in (first_str(x) for x in v) if s), "")
    return ""


def base_name(f):
    f = f.rstrip("/")
    return f.split("/")[-1] or f


def call_detail(nm, a):
    """A short phrase that says what a call did: its label next to the tool name."""
    if nm in ("read_file", "write_file", "patch"):
        return base_name(first_str(a.get("path") or a.get("file_path")))
    if nm == "search_files":
        return first_str(a.get("pattern"))
    if nm in ("terminal", "execute_code"):  # the command itself, not the cd in front of it
        s = first_str(a.get("command") or a.get("code"))
        s = re.sub(r"^(\s*(#[^\n]*\n|cd\s+\S+\s*(&&|;|\n)))+\s*", "", s)
        return re.sub(r"\s+", " ", s)
    if nm in ("web_search", "session_search", "crumb_find"):
        return first_str(a.get("query") or a.get("q"))
    if nm == "web_extract":
        u = first_str(a.get("urls") or a.get("url"))
        m = re.match(r"^[a-z]+://([^/?#]+)([^?#]*)", u)
        return m.group(1) + m.group(2) if m else u
    if nm in ("skill_view", "skill_manage"):
        return first_str(a.get("name"))
    if nm == "delegate_task":
        k = len(a["tasks"]) if isinstance(a.get("tasks"), list) else 1
        return "%d %s" % (k, "task" if k == 1 else "tasks")
    if nm == "message_agent":
        return first_str(a.get("target"))
    return next((s for s in (first_str(v) for v in a.values()) if s), "")


def call_targets(nm, a):
    """The files that a call read or wrote, read off its arguments."""
    f = first_str(a.get("path") or a.get("file_path"))
    return [] if not f else [(f, "read")] if nm == "read_file" else [(f, "write")] if nm in ("write_file", "patch") else []


def _json(t):
    try:
        v = json.loads(t)
    except ValueError:
        return None
    return v if isinstance(v, (dict, list)) else None


def as_json(s):
    """A JSON result; the store keeps long lines as several lines, so the breaks are joined first."""
    if not s.startswith("{"):
        return None
    for t in (s, s.replace("\n", "")):
        v = _json(t)
        if v is not None:
            return v
    return None


def step_calls(text):
    """A step's text: its narrative, then one line per call, "<name> <arguments>" (ingest.put_rows)."""
    lines, say, calls = tidy(text, True).split("\n"), [], []
    i = 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][\w.:-]*) (\{.*)$", lines[i])
        a, j = None, i
        if m:  # the store keeps long lines as several lines: a call's arguments end where they parse
            body = m.group(2)
            a = _json(body)
            while a is None and j + 1 < len(lines) and j - i < 400:
                j += 1
                body += lines[j]
                a = _json(body)
        if a is None:
            if not calls:
                say.append(lines[i])
            i += 1
            continue
        i = j + 1
        nm = bare_tool(m.group(1))
        if nm == "tool_call" and isinstance(a, dict) and isinstance(a.get("calls"), list) and a["calls"]:  # a deferred tool: its inner call
            c = a["calls"][0] if isinstance(a["calls"][0], dict) else {}
            ia = c.get("arguments") or c.get("args") or {}
            if isinstance(ia, str):
                ia = _json(ia) or {}
            calls.append({"nm": bare_tool(c.get("name")), "args": ia if isinstance(ia, dict) else {}})
        else:
            calls.append({"nm": nm, "args": a if isinstance(a, dict) else {}})
    return {"say": "\n".join(say).strip(), "calls": calls}


def tool_outcome(s):
    """"ok" or "failed", read off a tool result; "" when the result does not say."""
    s = (s or "").strip()
    if not s.startswith("{"):
        return "failed" if re.match(r"^(error|traceback)\b", s, re.I) else ""
    v = as_json(s)
    if not isinstance(v, dict):
        return ""
    bad = v.get("success") is False or (isinstance(v.get("error"), str) and v["error"].strip()) or \
        (isinstance(v.get("exit_code"), (int, float)) and not isinstance(v.get("exit_code"), bool) and v["exit_code"] != 0)
    return "failed" if bad else "ok"


def tool_gist(s):
    """The first lines of a tool result, without its JSON wrapper."""
    s = (s or "").strip()
    v = as_json(s)
    raw = re.sub(r'^\{"\w+": ?"', "", s).replace("\\n", "\n").replace('\\"', '"') if re.match(r'^\{"\w+": ?"', s) else s
    if isinstance(v, dict):
        body = next((v[k] for k in ("error", "output", "content", "result", "message", "summary", "text")
                     if isinstance(v.get(k), str) and v[k].strip()), "")
    else:
        body = raw
    return "\n".join([x.strip() for x in body.split("\n") if x.strip()][:6])


# ---- the program ------------------------------------------------------------------------------------------------

WAIT = object()   # a job yields WAIT when it can do nothing yet, anything else when it did some work
SIDE_COLS = 44    # the side panel is this many characters wide
VENN_COLS = 46    # and the Venn of the Context box
FPS = 30


def motion_on(arg=None, env=os.environ):
    """The camera setting: --motion 0 or BREADCRUMBS_MOTION=0 gives the camera of the old page."""
    v = str(arg if arg is not None else env.get("BREADCRUMBS_MOTION", "1")).strip().lower()
    return v not in ("0", "off", "no", "false")


class Doc:
    """The document of one session: the frames (one for each user turn), the bands (a run of tools of one phase), the cards."""

    def __init__(self, state="loading"):
        self.state, self.err = state, ""
        self.frames, self.bands, self.cards, self.edges, self.all = [], [], [], [], []
        self.by_crumb, self.W, self.H, self.k = {}, 1.0, 1.0, 1.0


class SpaceGraphKitty:
    """The native explorer. SpaceGraphKitty(root).run() takes the terminal; headless() and advance(), paint() and handle()
    drive it with no terminal (the tests, and the snapshots of Assignment E)."""

    def __init__(self, root, host="127.0.0.1", port=8765, motion=None, seed=1, out=None, size=None, fps=None):
        self.root, self.host, self.port = root, host, port   # host and port: kept for the command line; nothing listens
        self.view, self.out = None, out
        self.motion = motion_on(motion)
        self.rng = random.Random(seed)
        self.fps = float(fps or os.environ.get("BREADCRUMBS_FPS") or FPS)
        self.nodes, self.roots, self.edges = {}, [], []
        self.layout, self.dirty, self.temp = "grid", True, 1.0
        self.cam = Camera(self.motion)
        self.manual, self.stack, self.dropped = False, ["*"], 0
        self.jobs, self.redraw, self.running = [], True, False
        self.clock = time.monotonic
        self.t0 = self.clock()
        self.msg_text, self.hover, self.hover_xy, self.agent_ids, self.agent_line, self.last_agent = "", None, None, [], "", ""
        self.ctx_on = self.side_on = self.follow_on = False
        self.find_on, self.query = False, ""
        self.ctx_sid = self.ctx_shown = self.ctx_pin = ""
        self.ctx_at = self.hi_at = None
        self.ctx_data, self.ctx_rows, self.venn, self.ctx_key = None, {}, None, None
        self.gantt_scroll = self.venn_scroll = self.side_scroll = self.gantt_x = 0
        self.venn_h = 0
        self.side = None            # {kind, title, id, level, lines}
        self.said = {}              # (id, level) -> text | None (asked)
        self.boxes, self.links, self.labels, self.words, self.hits = [], [], [], [], []
        self.drag, self.pixel_mouse, self.last_buf, self.sent = None, False, None, 0
        self.frames, self.skipped = 0, 0
        self.timers = {"follow": 0.0, "context": 0.0}
        self.fd_in = self.old_termios = self.old_handlers = None
        self.opened = False
        self.cols, self.rows, self.xpix, self.ypix = size or (100, 30, 0, 0)
        self.resize(self.cols, self.rows, self.xpix, self.ypix)

    # -- the window ------------------------------------------------------------------------------------------

    def resize(self, cols, rows, xpix=0, ypix=0):
        self.cols, self.rows, self.xpix, self.ypix = max(10, cols), max(4, rows), xpix, ypix
        budget = int(os.environ.get("BREADCRUMBS_PIXELS") or 1300000)
        self.W, self.H, self.shrink, self.U = geometry(self.cols, self.rows, xpix, ypix, budget)
        if os.environ.get("BREADCRUMBS_UNIT", "").isdigit():
            self.U = max(1, min(4, int(os.environ["BREADCRUMBS_UNIT"])))  # the glyph scale: 1 (8 pixels high) to 4
        U = self.U
        self.CW, self.LH, self.BAR, self.TRAIL, self.MSG, self.HEAD = 8 * U, 12 * U, 22 * U, 16 * U, 14 * U, 14 * U
        self.TOP, self.OPEN = self.BAR + self.TRAIL, 150 * U
        self.canvas = Canvas(self.W, self.H)
        self.last_buf = None
        self.dirty = self.redraw = True
        self.resize_panels()

    def resize_panels(self):
        self.RIGHT = (SIDE_COLS * self.CW + 2 * self.CW) if self.side_on else 0
        self.CTX_H = int(self.H * 0.44) if self.ctx_on else 0
        self.BOTTOM = self.MSG + self.CTX_H
        self.redraw = True

    def mid_x(self):
        return (self.W - self.RIGHT) / 2

    def mid_y(self):
        return self.TOP + (self.H - self.TOP - self.BOTTOM) / 2

    def sx(self, x):
        return (x - self.cam.x) * self.cam.k + self.mid_x()

    def sy(self, y):
        return (y - self.cam.y) * self.cam.k + self.mid_y()

    def headless(self, cols=100, rows=30, xpix=1280, ypix=720):
        """A picture size with no terminal."""
        self.resize(cols, rows, xpix, ypix)
        if self.view is None:
            self.init_view()
        return self

    def say(self, text):
        self.msg_text = text
        self.redraw = True

    # -- the data (the page's api() calls, in jobs that share the frame loop) -----------------------------------

    def init_view(self):
        from .novis import Hide
        from .view import View
        self.view = View(self.root, hide=Hide(os.path.dirname(os.path.abspath(self.root))))  # no-vis sessions stay out
        self.load()

    def load(self):
        r = self.view.graph()
        for info in r["nodes"]:
            self.add_root(info)
        self.edges = [Edge(e["source"], e["target"], e["w"], e.get("shared") or []) for e in r["edges"] if e.get("kind") == "cue"]
        top = max([e.w for e in self.edges] + [1e-9])
        for e in self.edges:
            e.k = e.w / top   # 1 for the strongest link: a weak overlap still pulls
        self.dirty = True
        self.say("%d sessions. Click a box to zoom in, click it again to go back. Wheel zooms, drag pans, arrows move." % len(r["nodes"]))

    def add_root(self, info):
        if info["id"] in self.nodes:
            return self.nodes[info["id"]]
        n = Node(info["id"], info)
        a = self.rng.random() * 2 * math.pi
        d = (60 * math.sqrt(len(self.roots) + 1) + 80) * math.sqrt(self.rng.random())
        n.x, n.y = math.cos(a) * d, math.sin(a) * d
        n.s = 70 * (0.3 + 0.7 * (info.get("pri") or 0.5))
        n.w = n.h = n.s
        n.tx, n.ty = n.x, n.y
        self.nodes[n.key] = n
        self.roots.append(n)
        self.temp, self.dirty = max(self.temp, 0.5), True
        return n

    def spawn(self, gen):
        self.jobs.append(gen)

    def defer(self, fn):
        def run():
            fn()
            yield
        self.jobs.append(run())

    def pump(self, budget=0.006, limit=400):
        """Run jobs until the budget is spent (a job is a generator: each yield is a place to stop). Returns True if any ran."""
        t0, worked, steps = self.clock(), False, 0
        while self.jobs and steps < limit:
            progress = False
            for g in list(self.jobs):
                try:
                    r = next(g)
                except StopIteration:
                    self.jobs.remove(g)
                    progress = worked = True
                    continue
                except Exception as e:  # a failed call is a message, as in the page
                    self.jobs.remove(g)
                    self.say(str(e) or e.__class__.__name__)
                    progress = worked = True
                    continue
                steps += 1
                if r is not WAIT:
                    progress = worked = True
                if self.clock() - t0 > budget:
                    self.redraw = self.redraw or worked
                    return worked
            if not progress:
                break
        self.redraw = self.redraw or worked
        return worked

    def drain(self, limit=20000):
        """Run every job to its end (headless)."""
        for _ in range(limit):
            if not self.jobs:
                return
            self.pump(budget=1e9, limit=100000)

    def text(self, n, level):
        """The text of a box at a level (what the page's text(n, level) gives): the best level that has arrived, or None."""
        if level not in n.texts and level not in n.asked:
            n.asked.add(level)
            self.defer(lambda: n.texts.__setitem__(level, self.view.crumb(n.id, level)["text"]))
        best = None
        for lv in LEVELS:
            if lv <= level and lv in n.texts:
                best = n.texts[lv]
        return best

    def seen(self, cid, level):
        """crumb(id, level).text for the Context box and the side panel; None until it arrives, "" on an error."""
        k = (cid, level)
        if k not in self.said:
            self.said[k] = None

            def get():
                try:
                    self.said[k] = self.view.crumb(cid, level)["text"] or ""
                except (KeyError, ValueError):
                    self.said[k] = ""
            self.defer(get)
        return self.said[k]

    def parts(self, n):
        """Load the parts of a crumb once (n.kids is None until then)."""
        if n.loading:
            return n.kids
        n.loading = True
        if n.kind == "text" or ":" in n.id:
            n.kids = []
            return n.kids

        def get():  # a crumb that holds only texts (a turn) shows its text, not a grid
            try:
                kids = self.view.graph(root=n.id)["nodes"][1:]
            except (KeyError, ValueError) as e:
                self.say(str(e))
                kids = []
            if any(":" not in i["id"] for i in kids):
                n.kids = []
                for i, info in enumerate(kids):
                    k = Node("%s>%d.%s" % (n.key, i, info["id"]), info, n.key)
                    self.nodes[k.key] = k
                    n.kids.append(k.key)
            else:
                n.kids = []
        self.defer(get)
        return None

    # -- an open session is a document: frames of turns, bands of phases, cards of crumbs and files ----------------

    def doc(self, s):
        """The document of a session, built once: s.doc.state is loading, ready or error."""
        if not s.doc_ready:
            s.doc_ready = True
            s.doc = Doc("loading")
            self.spawn(self.build_doc_job(s))
        return s.doc

    def build_doc_job(self, s):
        yield  # let one frame show "Opening the session" first
        try:
            r = self.view.session(s.session)
        except (KeyError, ValueError) as e:
            s.doc = Doc("error")
            s.doc.err = str(e)
            self.say(str(e))
            return
        by_id = {x["id"]: x for x in r["nodes"]}
        kids = {}
        for e in r["edges"]:
            if e["kind"] == "kid":
                kids.setdefault(e["source"], []).append(e["target"])
        leaves, first_leaf, seen = [], {}, set()

        def walk(i):  # the leaves in order; each span keeps its first leaf
            if i in seen:
                return None
            seen.add(i)
            k = kids.get(i)
            if not k:
                if i != r["root"] and i in by_id:
                    leaves.append(by_id[i])
                    return i
                return None
            f = None
            for c in k:
                x = walk(c)
                f = f or x
            if f:
                first_leaf[i] = f
            return f
        walk(r["root"])
        steps, calls, last = [x for x in leaves if x["kind"] == "step"], {}, self.clock()
        for i, x in enumerate(steps):
            calls[x["id"]] = step_calls(self.view.crumb(x["id"], 1)["text"])
            if self.clock() - last > 0.004:
                last = self.clock()
                self.say("Opening the session: %d of %d steps read" % (i + 1, len(steps)))
                yield
        s.doc = self.build_doc(s, r, leaves, first_leaf, calls)
        self.dirty = self.redraw = True
        if len(steps) >= 40:
            self.say("%d crumbs in %d turns" % (len(leaves), sum(1 for f in s.doc.frames)))

    def build_doc(self, s, r, leaves, first_leaf, calls):
        """The layout of a session document (view.html buildDoc): a frame for each user turn, a band for each run of tools of one
        phase, a card for each crumb, and a card for each file that the tools read or wrote. Units are the page's world units."""
        turns, queue = [], {}
        for x in leaves:
            role = (x.get("meta") or {}).get("role") or ""
            if x["kind"] == "turn" and role == "user":
                turns.append({"prompt": x, "items": []})
                continue
            if not turns:
                turns.append({"prompt": None, "items": []})
            T = turns[-1]
            if x["kind"] == "step":
                c = calls.get(x["id"]) or {"say": "", "calls": []}
                queue[x["id"]] = list(c["calls"])
                T["items"].append({"kind": "narrative", "x": x, "say": c["say"]} if c["say"] else {"kind": "ref", "x": x})
            elif x["kind"] == "tool":
                step = (x.get("meta") or {}).get("step")
                q = queue.get(step) if step else None
                c = q.pop(0) if q else None
                nm = c["nm"] if c else bare_tool(re.sub(r"^tool:?", "", role))
                T["items"].append({"kind": "tool", "x": x, "nm": nm, "args": c["args"] if c else {}, "phase": PHASE_OF.get(nm, "result")})
            else:
                T["items"].append({"kind": "mem" if x["kind"] == "mem" else "sum" if x["kind"] == "sum" else "narrative", "x": x})
        d = Doc("ready")

        def add(key, info, lst):
            n = Node("%s>%s" % (s.key, key), dict({"in_doc": d, "sess": s.key}, **info), s.key)
            self.nodes[n.key] = n
            lst.append(n)
            return n

        def says(x):
            return re.sub(r"^(user|assistant|tool(:[^:\s]*)?):\s*", "", x.get("title") or "")
        files, wires = {}, []
        for ti, T in enumerate(turns):
            local, st = [], {"y": FHEAD + 6, "rx": 0, "row": 0, "count": 0}

            def put(n, x, y, w, h, local=local):
                n.rx, n.ry, n.rw, n.rh = x, y, w, h
                local.append(n)

            def tall(x):
                return 100 if ((x or {}).get("tokens") or 0) > 60 else CARD[1]

            def end_row(st=st):
                if st["rx"] > 0:
                    st["y"] += st["row"] + GY
                    st["rx"] = st["row"] = 0

            def prose(key, info, h, st=st, put=put, end_row=end_row):  # a prose card takes a whole row
                end_row()
                n = add(key, info, d.cards)
                put(n, FPAD, st["y"], ROWW, h)
                st["y"] += h + GY
                st["count"] += 1
                return n

            def band(key, info, k, ch, st=st, put=put, end_row=end_row):  # a band of k cards of height ch: where its cards go
                c, rows = min(COLS, k), -(-k // COLS)
                bw, bh = c * CARD[0] + (c - 1) * GX + 2 * BPAD, BHEAD + rows * (ch + GY) - GY + BPAD
                if st["rx"] > 0 and st["rx"] + bw > ROWW:
                    end_row()
                b = add(key, info, d.bands)
                bx, by = FPAD + st["rx"], st["y"]
                put(b, bx, by, bw, bh)
                st["rx"] += bw + GX
                st["row"] = max(st["row"], bh)
                return lambda i: (bx + BPAD + (i % COLS) * (CARD[0] + GX), by + BHEAD + (i // COLS) * (ch + GY))
            if T["prompt"]:
                x = T["prompt"]
                d.by_crumb[x["id"]] = prose("c:" + x["id"], {"id": x["id"], "kind": "prompt", "label": "user", "head": says(x),
                                                              "tokens": x.get("tokens", 0), "tip": says(x), "trail": "prompt: " + says(x)}, tall(x)).key
            segs = []
            for it in T["items"]:
                if it["kind"] == "ref":
                    continue  # a step without text: its tool cards stand for it
                last = segs[-1] if segs else None
                if it["kind"] == "tool" and last and last.get("band") == it["phase"]:
                    last["items"].append(it)
                else:
                    segs.append({"band": it["phase"], "items": [it]} if it["kind"] == "tool" else {"it": it})
            fresh = []
            for sg in segs:
                if "it" in sg:
                    it = sg["it"]
                    x = it["x"]
                    lab = "memory" if it["kind"] == "mem" else "summary (model text)" if it["kind"] == "sum" else "assistant"
                    head = it["say"].split("\n")[0] if it.get("say") else says(x)
                    d.by_crumb[x["id"]] = prose("c:" + x["id"], {"id": x["id"], "kind": it["kind"], "label": lab, "say": it.get("say") or "",
                                                                  "head": head, "tokens": x.get("tokens", 0),
                                                                  "tip": it["say"][:300] if it.get("say") else says(x),
                                                                  "trail": lab + ": " + head}, tall(x)).key
                    continue
                at = band("b%d.%d" % (ti, len(d.bands)), {"kind": sg["band"], "label": sg["band"], "count": len(sg["items"]), "synthetic": True,
                                                          "tip": "%s: %s" % (sg["band"], ", ".join(it["nm"] for it in sg["items"])[:300]),
                                                          "trail": "%s (turn %d)" % (sg["band"], ti + 1)}, len(sg["items"]), CARD[1])
                for i, it in enumerate(sg["items"]):
                    x, det = it["x"], call_detail(it["nm"], it["args"])
                    n = add("c:" + x["id"], {"id": x["id"], "kind": it["phase"], "label": it["nm"], "detail": det, "mono": True,
                                             "tokens": x.get("tokens", 0),
                                             "tip": it["nm"] + (" " + det if det else "") + "\n" + first_str([first_str(v) for v in it["args"].values()])[:400],
                                             "trail": it["nm"] + (" " + det[:40] if det else "")}, d.cards)
                    d.by_crumb[x["id"]] = n.key
                    step = (x.get("meta") or {}).get("step")
                    if step and step not in d.by_crumb:
                        d.by_crumb[step] = n.key
                    cx, cy = at(i)
                    put(n, cx, cy, CARD[0], CARD[1])
                    for f, how in call_targets(it["nm"], it["args"]):
                        if f not in files:
                            files[f] = {"n": None, "read": 0, "write": 0}
                            fresh.append(f)
                        files[f][how] += 1
                        wires.append((n, f, how))
                    st["count"] += 1
            if fresh:  # the files that this turn touched first; later turns link to them here
                end_row()
                at = band("b%d.files" % ti, {"kind": "file", "label": "files", "count": len(fresh), "synthetic": True,
                                             "tip": "\n".join(fresh), "trail": "files (turn %d)" % (ti + 1)}, len(fresh), FILE_H)
                for i, f in enumerate(fresh):
                    n = add("file:" + f, {"kind": "file", "label": base_name(f), "mono": True, "synthetic": True, "path": f, "tip": f,
                                          "trail": "file " + base_name(f)}, d.cards)
                    files[f]["n"] = n
                    cx, cy = at(i)
                    put(n, cx, cy, CARD[0], FILE_H)
            end_row()
            fr = add("f%d" % ti, {"kind": "turn", "label": says(T["prompt"]) if T["prompt"] else "before the first prompt",
                                  "count": st["count"], "synthetic": True, "tip": says(T["prompt"]) if T["prompt"] else "",
                                  "trail": "turn %d%s" % (ti + 1, ": " + says(T["prompt"])[:40] if T["prompt"] else "")}, d.frames)
            fr.local, fr.rw, fr.rh = local, FRAME, st["y"] - GY + FPAD
        for f, v in files.items():
            dir_ = f[:f.rindex("/")] if "/" in f else ""
            v["n"].sum = " \u00b7 ".join(x for x in ("%d %s" % (v["read"], "read" if v["read"] == 1 else "reads") if v["read"] else "",
                                                     "%d %s" % (v["write"], "write" if v["write"] == 1 else "writes") if v["write"] else "") if x) \
                + ("\n" + dir_ if dir_ else "")
        for n, f, how in wires:
            d.edges.append((n, files[f]["n"], how))
        for span, leaf in first_leaf.items():
            if span not in d.by_crumb and leaf in d.by_crumb:
                d.by_crumb[span] = d.by_crumb[leaf]
        fs = d.frames  # the frames in rows, so that the document is about as wide as it is tall (it sits in a square box)
        per, score = 1, float("inf")
        for k in range(1, max(1, len(fs)) + 1):
            h = sum(max(f.rh for f in fs[i:i + k]) + FGAP for i in range(0, len(fs), k)) if fs else 1
            sc = abs(math.log((k * FRAME + (k - 1) * FGAP) / max(1, h - FGAP)))
            if sc < score:
                score, per = sc, k
        fy = 0.0
        for i in range(0, len(fs), per):
            row = fs[i:i + per]
            for j, f in enumerate(row):
                fx = j * (FRAME + FGAP)
                f.rx, f.ry = fx, fy
                for n in f.local:
                    n.rx += fx
                    n.ry += fy
            fy += max(f.rh for f in row) + FGAP
        n_per = max(1, min(per, len(fs)))
        d.W, d.H = max(1, n_per * FRAME + (n_per - 1) * FGAP), max(1, fy - FGAP)
        d.all = d.frames + d.bands + d.cards
        return d

    def doc_place(self, s, d):
        """World rects of the document inside its session's box (the box can still move)."""
        w, h = s.w or s.s, s.h or s.s
        m = min(w, h)
        left, top, aw, ah = s.x - w * 0.47, s.y - h * 0.5 + m * 0.085, w * 0.94, h - m * 0.115
        k = min(aw / d.W, ah / d.H)
        ox = left + (aw - d.W * k) / 2
        d.k = k
        for n in d.all:
            n.w, n.h = n.rw * k, n.rh * k
            n.x, n.y = ox + n.rx * k + n.w / 2, top + n.ry * k + n.h / 2

    # -- drawing: the map (view.html draw, drawNode, drawText, drawLabels) -----------------------------------------

    def clip(self, x0, y0, x1, y1):
        """A with-block that clips to a rectangle inside the current clip."""
        return _Clip(self.canvas, x0, y0, x1, y1)

    def fit(self, s, cols):
        """The text cut to a width in characters, with an ellipsis."""
        s = plain(s)
        cols = int(cols)
        if cols <= 0:
            return ""
        return s if len(s) <= cols else (s[:cols - 1] + "\u2026" if cols > 1 else s[:1])

    def paint(self):
        """One frame as a Canvas: the map under the toolbar, the trail, the Context box, the side panel and the status line."""
        cv, t = self.canvas, self.clock()
        cv.clear()
        self.boxes, self.links, self.labels, self.words, self.hits = [], [], [], [], []
        cv.clip = (0, self.TOP, max(0, self.W - self.RIGHT), max(self.TOP, self.H - self.BOTTOM))
        self.draw_map(t)
        cv.clip = (0, 0, self.W, self.H)
        self.draw_chrome(t)
        return cv

    def draw_map(self, t):
        cv, hover = self.canvas, self.hover
        if self.layout == "radial":
            for a in self.roots:
                if a.rings and not a.tup:
                    for r in a.rings:  # the rings round each hub
                        cx, cy, rr = self.sx(a.x), self.sy(a.y), r * self.cam.k
                        if rr < 20000:
                            pts = [(cx + rr * math.cos(i * math.pi / 32), cy + rr * math.sin(i * math.pi / 32)) for i in range(65)]
                            for p, q in zip(pts, pts[1:]):
                                cv.line(p[0], p[1], q[0], q[1], LINE)
        under = lambda e: self.layout == "force" or e.tree  # noqa: E731
        for e in self.edges:
            if under(e):
                self.draw_edge(e)
        for a in self.roots:
            if a.kids:
                layout_kids(a, self.nodes)
        for a in self.roots:
            self.draw_node(a, t, 0)
        for e in self.edges:  # a fixed layout draws the other links of the box under the pointer over the boxes
            if not under(e) and hover and hover.key in (e.source, e.target):
                self.draw_edge(e)
        pts = [self.nodes[k] for k in self.stack[1:] if k in self.nodes]
        for a, b in zip(pts, pts[1:]):  # the trail between boxes; a step into a part of the same box is not drawn
            if b.key.startswith(a.key + ">") or a.key.startswith(b.key + ">") or (a.sess and a.sess == b.sess):
                continue
            cv.dashed(self.sx(a.x), self.sy(a.y), self.sx(b.x), self.sy(b.y), mix(ACCENT, BG, 0.85), 6 * self.U, 10 * self.U, 0, 2)
        self.draw_labels()

    def draw_edge(self, e):
        A, B = self.nodes.get(e.source), self.nodes.get(e.target)
        if not A or not B:
            return
        cv, hover = self.canvas, self.hover
        on = bool(hover and hover.key in (e.source, e.target))
        col, w = (ACCENT, 2) if on else (mix(DIM, BG, 0.22 + 0.4 * e.k), 2 if e.k > 0.6 else 1)
        ax, ay, bx, by = self.sx(A.x), self.sy(A.y), self.sx(B.x), self.sy(B.y)
        if e.tree and self.layout == "tree":  # an elbow, as in an org chart
            m = self.sy((A.y + B.y) / 2)
            cv.line(ax, ay, ax, m, col, w)
            cv.line(ax, m, bx, m, col, w)
            cv.line(bx, m, bx, by, col, w)
        else:
            cv.line(ax, ay, bx, by, col, w)
        if e.shared:
            self.words.append({"a": (ax, ay), "b": (bx, by), "s": " ".join(e.shared[:2]), "on": on})

    def draw_text(self, n, x, y, pw, bh, bg):
        """The text of a box at the level that its area holds (semantic zoom). False if the box is too small for any."""
        U, CW, LH, cv = self.U, self.CW, self.LH, self.canvas
        cols, rows = int((pw - 12 * U) // CW), int((bh - 8 * U) // LH)
        if cols < 12 or rows < 2:
            return False
        raw = self.text(n, pick(n.tokens, cols * rows)) or handle(n)
        lines = [w for l in plain(raw).split("\n") for w in wrap(l, cols)]
        with self.clip(x, y, x + pw, y + bh):
            for i in range(min(rows, len(lines))):
                ly = y + 6 * U + LH * i
                if ly + 8 * U < self.TOP:
                    continue
                if ly > self.H:
                    break
                m = re.match(r"^\s*(\u25b8|\u22ef) (\S+)", lines[i])
                line = lines[i]
                if m:  # a handle or a gap: a link to its ref. It shows its kind and title, not its id and size
                    line = _HANDLE.sub(lambda q: (q.group(1) + "\u25b8 " + q.group(2) + " " + (q.group(3) or "") + q.group(4)).rstrip(), line)
                    line = re.sub(r"^(\s*)\u22ef \S+", "\\1\u22ef", line)
                if len(line) > cols:
                    line = line[:cols - 1] + "\u2026"
                cv.text(x + 6 * U, ly, line, (DIM if m.group(1) == "\u22ef" else ACCENT) if m else FG, bg, U)
                if m:
                    self.links.append((x + 6 * U, ly - U, len(line) * CW, LH, m.group(2), n))
        return True

    def draw_node(self, n, t, depth):
        """A box: a session is a square, or a rectangle in the treemap; a part is a square."""
        cam, U, cv = self.cam, self.U, self.canvas
        pw, ph = (n.w or n.s) * cam.k, (n.h or n.s) * cam.k
        px_ = min(pw, ph)
        x, y = self.sx(n.x) - pw / 2, self.sy(n.y) - ph / 2
        if not (x == x and y == y) or x > self.W or y > self.H or x + pw < 0 or y + ph < self.TOP or px_ < 1.5:
            return
        c = kind_color(n.kind)
        lit = n.glow > t or n.id in self.agent_ids
        top = self.stack[-1] == n.key
        fill = mix(c, CARD_BG, 0.2 if lit else 0.06 + 0.03 * depth)
        rad = min(7 * U, px_ * 0.1)
        if lit and depth == 0 and px_ > 5:  # the glow of a box that the agent opened or a find hit
            cv.rrect(x - 3 * U, y - 3 * U, pw + 6 * U, ph + 6 * U, rad + 3 * U, mix(AGENT, BG, 0.28))
        cv.frame(x, y, pw, ph, rad, fill, ACCENT if top else AGENT if lit else mix(c, BG, 0.6), 2 if top else 1)
        self.boxes.append((n, x, y, pw, ph, depth))
        hy = 0
        if px_ >= 100 * U and y + self.HEAD > self.TOP:  # the header: a dot of the kind's colour, the kind, the cue words
            hy = self.HEAD
            with self.clip(x, y, x + pw, y + self.HEAD):
                cv.disc(x + 7 * U, y + self.HEAD / 2, 2.6 * U, c)
                head = (n.kind.upper() + "   " + " ".join(n.cues[:4])).strip()
                cv.text(x + 14 * U, y + (self.HEAD - 8 * U) / 2, self.fit(head, (pw - 18 * U) // self.CW), FG, fill, U)
            cv.rect(x + 1, y + self.HEAD, pw - 2, 1, mix(c, fill, 0.25))
        elif px_ >= 22 * U:  # a small box keeps a dot of its kind, as a SpaceGraph node keeps its icon
            cv.disc(x + 7 * U, y + 7 * U, min(3 * U, px_ / 10), c)
        if n.kind == "sess" and n.session and not (n.doc and n.doc.state == "error"):  # a session opens as a document
            if px_ >= self.OPEN and (n.doc_ready or n.key in self.stack or (self.manual and px_ >= 0.8 * min(self.W, self.H))):  # a zoom that passes by builds none
                d = self.doc(n)
                if d.state == "ready":
                    return self.draw_doc(n, d, t)
                if d.state == "loading":
                    cv.text(x + 14 * U, max(self.TOP + 24 * U, y + hy + 24 * U), "Opening the session\u2026", DIM, fill, U)
                    return
            if self.draw_text(n, x, y + hy, pw, ph - hy, fill):
                n.shown = True
            elif depth == 0:
                self.labels.append({"n": n, "x": x + pw / 2, "y": y + ph, "px": pw, "ph": ph})
            return
        if px_ >= self.OPEN:
            self.parts(n)
        opened = px_ >= self.OPEN and n.kids
        if self.draw_text(n, x, y + hy, pw, (ph * 0.35 if opened else ph) - hy, fill):
            n.shown = True
        elif depth > 0 and px_ >= 8 * self.CW:  # a small part shows its cue words (the colour gives its kind)
            with self.clip(x, y, x + pw, y + ph):
                cv.text(x + 4 * U, y + ph / 2 - 4 * U, self.fit(name(n)[len(n.kind) + 1:], (pw - 6 * U) // self.CW), FG, fill, U)
        elif depth == 0:
            self.labels.append({"n": n, "x": x + pw / 2, "y": y + ph, "px": pw, "ph": ph})
        if opened:
            layout_kids(n, self.nodes)
            for key in n.kids:
                self.draw_node(self.nodes[key], t, depth + 1)

    def draw_labels(self):
        """Biggest boxes first; a label goes below, above, right or left, where it overlaps nothing."""
        cv, U, CW = self.canvas, self.U, self.CW
        placed = [(b[1], b[2], b[1] + b[3], b[2] + b[4]) for b in self.boxes if b[5] == 0]
        x_max, y_max = self.W - self.RIGHT, self.H - self.BOTTOM

        def free(r):
            return r[0] >= 0 and r[2] <= x_max and r[1] >= self.TOP and r[3] <= y_max and \
                not any(p[0] < r[2] and r[0] < p[2] and p[1] < r[3] and r[1] < p[3] for p in placed)

        def clear(r):  # no link crosses it
            return not any(clip_line(l["a"][0], l["a"][1], l["b"][0], l["b"][1], r[0], r[1], r[2], r[3]) for l in self.words)
        if self.labels and self.roots:
            self.labels.sort(key=lambda l: -l["px"])
            cell = (max(a.s for a in self.roots) + PAD) * self.cam.k  # the width of a cell of a fixed layout
            for l in self.labels:
                n = l["n"]
                s = self.fit((name(n)[5:] if n.kind == "sess" else name(n))[:36], (self.W if self.layout == "force" else cell) // CW)
                w, h = len(s) * CW, l["px"] / 2
                spots = [(l["x"] - w / 2, l["y"] + 4 * U), (l["x"] - w / 2, l["y"] - l["ph"] - 14 * U),
                         (l["x"] + h + 6 * U, l["y"] - l["ph"] / 2 - 4 * U), (l["x"] - h - w - 6 * U, l["y"] - l["ph"] / 2 - 4 * U)]
                rects = [(x - 2 * U, y, x + w + 2 * U, y + 12 * U) for x, y in spots]
                r = next((r for r in rects if free(r) and clear(r)), None) or next((r for r in rects if free(r)), None)
                if r and s:
                    placed.append(r)
                    cv.text(r[0] + 2 * U, r[1] + 2 * U, s, mix(FG, BG, 0.85), BG, U)
        for l in self.words:  # a cue edge names its shared words near its middle, in a pill that overlaps nothing
            w = len(plain(l["s"])) * CW
            for f in (0.5, 0.4, 0.6, 0.3, 0.7):
                x, y = l["a"][0] + f * (l["b"][0] - l["a"][0]), l["a"][1] + f * (l["b"][1] - l["a"][1])
                r = (x - w / 2 - 6 * U, y - 7 * U, x + w / 2 + 6 * U, y + 7 * U)
                if not free(r):
                    continue
                placed.append(r)
                cv.frame(r[0], r[1], r[2] - r[0], r[3] - r[1], 7 * U, PANEL, ACCENT if l["on"] else LINE2)
                cv.text(r[0] + 6 * U, y - 4 * U, plain(l["s"]), ACCENT if l["on"] else DIM, PANEL, U)
                break

    # -- drawing: the session document (view.html drawDoc, docFrame, docBand, docCard) ------------------------------

    def draw_doc(self, s, d, t):
        self.doc_place(s, d)
        cam, U, cv = self.cam, self.U, self.canvas
        z = d.k * cam.k

        def R(n):
            return self.sx(n.x - n.w / 2), self.sy(n.y - n.h / 2), n.w * cam.k, n.h * cam.k
        pw, ph = (s.w or s.s) * cam.k, (s.h or s.s) * cam.k
        hd = min(self.HEAD, min(pw, ph) * 0.085)  # the session's box: the canvas of the document
        cv.rrect(self.sx(s.x) - pw / 2 + 1, self.sy(s.y) - ph / 2 + hd + 1, pw - 2, ph - hd - 2, 6, BG)

        def on(r):
            return r[0] < self.W and r[1] < self.H and r[0] + r[2] > 0 and r[1] + r[3] > self.TOP and r[2] >= 1
        for n in d.frames:
            r = R(n)
            if on(r):
                self.doc_frame(n, r, z)
                self.boxes.append((n, r[0], r[1], r[2], r[3], 1))
        for n in d.bands:
            r = R(n)
            if on(r):
                self.doc_band(n, r, z)
                self.boxes.append((n, r[0], r[1], r[2], r[3], 2))
        if CARD[0] * z >= 40 * U:
            for e in d.edges:
                self.doc_edge(e)
        for n in d.cards:
            r = R(n)
            if on(r) and self.doc_card(n, r, z, t) is not False:
                self.boxes.append((n, r[0], r[1], r[2], r[3], 3))

    def doc_head(self, label, count, hue, x, y, w, bg):
        """A frame's or a band's head: a dot, its name, how many it holds. y is the top of the text."""
        U, CW, cv = self.U, self.CW, self.canvas
        cnt = str(count)
        cv.disc(x + 8 * U, y + 4 * U, 2.4 * U, hsl(hue, 0.7, 0.70))
        cv.text(x + 15 * U, y, self.fit(label.upper(), (w - 30 * U) // CW - len(cnt) - 1), FG, bg, U)
        cv.text(x + w - 8 * U - len(cnt) * CW, y, cnt, DIM, bg, U)

    def doc_frame(self, n, r, z):
        x, y, w, h = r
        U, cv = self.U, self.canvas
        top = self.stack[-1] == n.key
        base = hsl(HUE["turn"])
        fill = mix(base, BG, 0.035)
        cv.frame(x, y, w, h, max(2, min(12, 10 * z)), fill, ACCENT if top else mix(base, BG, 0.3), 2 if top else 1)
        small = CARD[0] * z < 50 * U
        head = FHEAD * z
        if w < 60 * U or (head < 11 * U and not small):  # too low for its head: the prompt card says it
            return
        with self.clip(x, y, x + w, y + h):
            if head < 14 * U:  # overview: the head sits on a pill over the first row
                cv.rrect(x + 2 * U, y + 2 * U, w - 4 * U, 12 * U, 4 * U, PANEL)
                self.doc_head(n.label, n.count, HUE["turn"], x, y + 4 * U, w, PANEL)
            else:
                self.doc_head(n.label, n.count, HUE["turn"], x, y + max(4 * U, head * 0.64 - 8 * U), w, fill)

    def doc_band(self, n, r, z):
        x, y, w, h = r
        U, cv = self.U, self.canvas
        hue = HUE.get(n.kind, 220)
        top = self.stack[-1] == n.key
        tiny = CARD[0] * z < 50 * U  # tiny: the band stands for its cards
        base = hsl(hue)
        fill = mix(base, BG, 0.24 if tiny else 0.07)
        cv.frame(x, y, w, h, max(2, min(10, 8 * z)), fill, ACCENT if top else mix(base, BG, 0.34), 2 if top else 1)
        if w < 40 * U or h < 12 * U or (not tiny and BHEAD * z < 11 * U):
            return
        with self.clip(x, y, x + w, y + h):
            self.doc_head(n.label, n.count, hue, x, y + (min(h / 2 - 4 * U, 5 * U) if tiny else max(4 * U, BHEAD * z * 0.66 - 8 * U)), w, fill)

    def doc_edge(self, e):
        """A tool card to the file that it read (blue) or wrote (green)."""
        a, b, how = e
        cv = self.canvas
        ax, ay, bx, by = self.sx(a.x), self.sy(a.y + a.h / 2), self.sx(b.x), self.sy(b.y - b.h / 2)
        if (ax < 0 and bx < 0) or (ax > self.W and bx > self.W) or (ay < self.TOP and by < self.TOP) or (ay > self.H and by > self.H):
            return
        my = (ay + by) / 2
        col = mix(hsl(HUE["edit"]), BG, 0.55) if how == "write" else mix(hsl(HUE["explore"]), BG, 0.35)
        pts = []
        for i in range(17):  # the page's bezier: (ax, ay) (ax, my) (bx, my) (bx, by)
            u = i / 16.0
            q = 1 - u
            pts.append((q ** 3 * ax + 3 * q * q * u * ax + 3 * q * u * u * bx + u ** 3 * bx,
                        q ** 3 * ay + 3 * q * q * u * my + 3 * q * u * u * my + u ** 3 * by))
        for p, q in zip(pts, pts[1:]):
            cv.line(p[0], p[1], q[0], q[1], col)

    def doc_full(self, n):
        """A tool result at level 1, for its outcome and its first lines (results up to 25,000 tokens)."""
        if getattr(n, "full", None) is None and not getattr(n, "full_asked", False) and (n.tokens or 0) <= 25000:
            n.full_asked = True

            def get():
                n.full = tidy(self.view.crumb(n.id, 1)["text"], True)
                n.st, n.gist = tool_outcome(n.full), tool_gist(n.full)
            self.defer(get)

    def doc_card(self, n, r, z, t):
        x, y, w, h = r
        U, CW, LH, cv = self.U, self.CW, self.LH, self.canvas
        hue = HUE.get(n.kind, 220)
        base = hsl(hue)
        top = self.stack[-1] == n.key
        lit = n.glow > t or n.id in self.agent_ids
        small = CARD[0] * z < 50 * U
        if small and n.mono:
            return False  # overview: its band stands for it
        if w < 20 * U or h < 7 * U:
            cv.rect(x, y, max(1, w), max(1, h), mix(base, BG, 0.95 if lit else 0.55))
            return True
        border = ACCENT if top else AGENT if lit else mix(base, CARD_BG, 0.45)
        if small:  # overview: a prose card is one line, its label and its first words
            fill = mix(base, CARD_BG, 0.1)
            cv.frame(x, y, w, h, 3, fill, border)
            if w >= 70 * U:
                with self.clip(x, y, x + w, y + h):
                    ty = y + max(1.0, (min(h, 14 * U) - 8 * U) / 2)
                    lab = plain(n.label)
                    cv.text(x + 5 * U, ty, lab, hsl(hue, 0.7, 0.76), fill, U)
                    lw = len(lab) * CW + 5 * U
                    cv.text(x + 5 * U + lw, ty, self.fit(n.head or "", (w - 12 * U - lw) // CW), FG, fill, U)
            return True
        u = min(1.15, z)
        tool = n.mono and n.kind != "file"
        fill = mix(base, CARD_BG, 0.07)
        if lit:
            cv.rrect(x - 3 * U, y - 3 * U, w + 6 * U, h + 6 * U, 6 * U, mix(AGENT, BG, 0.3))
        cv.frame(x, y, w, h, max(2, min(8, 7 * u)), fill, border, 2 if top else 1)
        if tool and w >= 110 * U:
            self.doc_full(n)
        pad = max(3 * U, 6 * U * min(u, 1.15))
        chip = 0 if w < 80 * U else 12 * U
        with self.clip(x, y, x + w, y + h):
            if chip:
                cv.rrect(x + pad, y + pad, chip, chip, 3 * U, mix(base, fill, 0.2))
                g = GLYPH.get(n.kind, "\u2022")
                cv.text(x + pad + (chip - 8 * U) / 2, y + pad + (chip - 8 * U) / 2, g, hsl(hue, 0.7, 0.74), mix(base, fill, 0.2), U)
            st = getattr(n, "st", "")
            if st:  # the outcome as a corner dot
                cv.disc(x + w - pad - 3 * U, y + pad + 4 * U, 3.2 * U, GOOD if st == "ok" else BAD)
            if h >= 12 * U and w >= 40 * U:
                lx = x + pad + (chip + 5 * U if chip else 0)
                ly = y + pad + (chip - 8 * U) / 2 if chip else y + pad
                room = int((x + w - lx - pad - (9 * U if st else 0)) // CW)
                lab = self.fit(n.label, room)
                cv.text(lx, ly, lab, FG if n.mono else hsl(hue, 0.7, 0.76), fill, U)
                lw = (len(lab) + 1) * CW
                body_y = max(y + pad + chip, ly + 8 * U) + 3 * U
                if not n.mono:
                    self.doc_prose(n, lx + lw, ly, room * CW - lw, x + pad, w - 2 * pad, y + h - pad * 0.5, fill)
                else:
                    if n.detail:
                        cv.text(lx + lw, ly, self.fit(n.detail, (room * CW - lw) // CW), DIM, fill, U)
                    self.doc_body(n, x + pad, body_y, w - 2 * pad, y + h - pad * 0.5 - body_y, fill)

    def doc_prose(self, n, x1, y1, w1, x2, w2, y_end, bg):
        """A prose card's text: it starts after the label and wraps under it."""
        U, CW, LH, cv = self.U, self.CW, self.LH, self.canvas
        rows = 1 + max(0, int((y_end - y1 - LH * 0.3 - 8 * U) // LH))
        cap = max(1, int((max(0, w1) + (rows - 1) * w2) // CW))
        s = n.say if n.say and cap < 900 else None
        if s is None:
            tx = self.text(n, pick(n.tokens, cap))
            s = (n.head or "\u2026") if tx is None else tidy(tx, True)
        out, cur, width, more = [], "", max(0, w1), False
        for para in plain(s).split("\n"):
            for wd in para.split():
                trial = cur + " " + wd if cur else wd
                if len(trial) * CW <= width or (not cur and out):
                    cur = trial
                else:
                    out.append(cur)
                    cur, width = wd, w2
                if len(out) >= rows:
                    more = True
                    break
            if len(out) >= rows:
                more = True
                break
            if cur:
                out.append(cur)
                cur, width = "", w2
        if cur and len(out) < rows:
            out.append(cur)
        for i, line in enumerate(out[:rows]):
            room = int((w2 if i else w1) // CW)
            last = i == rows - 1 and more
            cv.text(x2 if i else x1, y1 + i * LH, self.fit(line + " \u2026" if last else line, room), FG, bg, U)

    def doc_body(self, n, x, y, w, h, bg):
        """What the crumb says, at the level that this card can hold."""
        U, CW, LH, cv = self.U, self.CW, self.LH, self.canvas
        cols, rows = int(w // CW), int(h // LH)
        if cols < 6 or rows < 1:
            return
        prose = not n.mono
        s = None
        if n.kind == "file":
            s = getattr(n, "sum", "")
        elif n.say and rows * cols < 900:
            s = n.say
        elif n.mono and rows * cols < 900 and getattr(n, "gist", None) is not None:
            s = n.gist
        if s is None:
            tx = self.text(n, pick(n.tokens, cols * rows))
            s = "\u2026" if tx is None else tidy(tx, True)
            if n.mono and re.match(r'^\{"\w+": ?"', s):
                s = re.sub(r'^\{"\w+": ?"', "", s).replace("\\n", "\n").replace('\\"', '"').replace("\\t", "  ")
        lines = [w_ for l in plain(s).split("\n") for w_ in wrap(l, cols)]
        for i in range(min(rows, len(lines))):
            line = lines[i]
            if len(line) > cols or (i == rows - 1 and len(lines) > rows):
                line = line[:max(0, cols - 1)] + "\u2026"
            cv.text(x, y + LH * i, line, FG if prose else DIM, bg, U)


    # -- drawing: the chrome (toolbar, trail, status, Context box, side panel, tip) --------------------------------------

    def draw_chrome(self, t):
        self.draw_toolbar()
        self.draw_trail()
        if self.ctx_on:
            self.draw_context()
        if self.side_on:
            self.draw_side()
        self.draw_status()
        self.draw_tip()

    def tool_button(self, x, label, action, arg=None, on=False):
        """A button of the toolbar at x; returns the x of the next one."""
        cv, U, CW = self.canvas, self.U, self.CW
        bw, bh, y0 = len(label) * CW + 12 * U, 14 * U, (self.BAR - 14 * U) // 2
        bg = mix(ACCENT, PANEL, 0.13) if on else PANEL
        if on:
            cv.frame(x, y0, bw, bh, 5 * U, bg, mix(ACCENT, PANEL, 0.35))
        cv.text(x + 6 * U, y0 + (bh - 8 * U) // 2, label, FG if on else DIM, bg, U)
        self.hits.append((x, y0, x + bw, y0 + bh, action, arg))
        return x + bw + 2 * U

    def draw_toolbar(self):
        cv, U, CW, W = self.canvas, self.U, self.CW, self.W
        cv.rect(0, 0, W, self.BAR, PANEL)
        cv.rect(0, self.BAR - 1, W, 1, LINE)
        x, y0 = 10 * U, (self.BAR - 14 * U) // 2
        cv.rrect(x, y0, 14 * U, 14 * U, 4 * U, ACCENT)
        cv.text(x + 3 * U, y0 + 3 * U, "b", BG, ACCENT, U)
        cv.text(x + 20 * U, y0 + 3 * U, "breadcrumbs", FG, PANEL, U)
        x += 20 * U + 11 * CW + 12 * U
        x = self.tool_button(x, "Overview", "home")
        x = self.tool_button(x, "Context", "context", on=self.ctx_on)
        x = self.tool_button(x, "Follow the agent", "follow", on=self.follow_on)
        x += 6 * U
        cv.rect(x, y0, 1, 14 * U, LINE)
        x += 7 * U
        for key, label in LAYOUTS:
            x = self.tool_button(x, label, "layout", key, on=self.layout == key)
        fw = 22 * CW
        fx = W - fw - 10 * U
        cv.frame(fx, y0, fw, 14 * U, 5 * U, BG, ACCENT if self.find_on else LINE2)
        shown = self.query + ("_" if self.find_on else "")
        cv.text(fx + 7 * U, y0 + 3 * U, self.fit(shown or "Find in memory", (fw - 14 * U) // CW),
                FG if self.query or self.find_on else DIM, BG, U)
        self.hits.append((fx, y0, fx + fw, y0 + 14 * U, "find", None))
        if self.agent_line:
            room = (fx - x - 14 * U) // CW
            if room > 12:
                cv.disc(fx - 10 * U - min(room, len(self.agent_line) + 7) * CW, y0 + 7 * U, 3 * U, AGENT)
                s = self.fit("Agent  " + self.agent_line, room - 2)
                cv.text(fx - 6 * U - len(s) * CW, y0 + 3 * U, s, AGENT, PANEL, U)

    def draw_trail(self):
        cv, U, CW, W = self.canvas, self.U, self.CW, self.W
        y0 = self.BAR
        cv.rect(0, y0, W, self.TRAIL, BG)
        cv.rect(0, y0 + self.TRAIL - 1, W, 1, LINE)
        ty, x = y0 + (self.TRAIL - 8 * U) // 2, 14 * U
        legend = ["prompt", "narrative", "explore", "edit", "verify", "delegate", "result", "file"]
        lw = sum(len(k) * CW + 18 * U for k in legend)
        end = (W - lw - 14 * U) if W > lw + 400 * U else W - 14 * U
        for i, key in enumerate(self.stack):
            n = self.nodes.get(key)
            label = "All sessions" if key == "*" else name(n) if n else key
            label = self.fit(label, max(4, (end - x) // CW))
            if i == 1 and self.dropped:
                cv.text(x, ty, "\u2026", FAINT, BG, U)
                x += CW + 8 * U
            last = i == len(self.stack) - 1
            cv.text(x, ty, label, FG if last else DIM, BG, U)
            self.hits.append((x, y0, x + len(label) * CW, y0 + self.TRAIL, "trail", i))
            x += len(label) * CW
            if not last:
                cv.text(x + 4 * U, ty, "/", FAINT, BG, U)
                x += CW + 8 * U
            if x > end:
                break
        if W > lw + 400 * U:  # the phase of each tool card, by tool name
            x = W - lw - 14 * U + 10 * U
            for k in legend:
                cv.rrect(x, ty, 8 * U, 8 * U, 2 * U, hsl(HUE[k], 0.7, 0.66))
                cv.text(x + 13 * U, ty, k, DIM, BG, U)
                x += len(k) * CW + 18 * U

    def draw_status(self):
        cv, U, CW, W, H = self.canvas, self.U, self.CW, self.W, self.H
        y0 = H - self.MSG
        cv.rect(0, y0, W, self.MSG, PANEL)
        cv.rect(0, y0, W, 1, LINE)
        cv.text(14 * U, y0 + (self.MSG - 8 * U) // 2, self.fit(self.msg_text, (W - 28 * U) // CW), DIM, PANEL, U)

    def tip_text(self, n):
        return n.tip if n.in_doc else handle(n) + ("\n" + n.title if n.title else "")

    def draw_tip(self):
        n, xy = self.hover, self.hover_xy
        if not n or not xy or self.drag or self.find_on:
            return
        cv, U, CW, LH = self.canvas, self.U, self.CW, self.LH
        cols = min(64, max(12, (self.W // 2) // CW))
        lines = [w for l in plain(self.tip_text(n) or "").split("\n") for w in wrap(l, cols)][:12]
        if not lines:
            return
        bw, bh = max(len(l) for l in lines) * CW + 14 * U, len(lines) * LH + 10 * U
        x, y = min(xy[0] + 14 * U, self.W - bw - 4 * U), xy[1] + 16 * U
        if y + bh > self.H - self.MSG:
            y = max(self.TOP, xy[1] - bh - 8 * U)
        cv.frame(x, y, bw, bh, 5 * U, CARD_BG, LINE2)
        for i, l in enumerate(lines):
            cv.text(x + 7 * U, y + 5 * U + i * LH, l, FG, CARD_BG, U)

    # -- the side panel: the text of the crumb or the event that was picked ---------------------------------------------

    def draw_side(self):
        cv, U, CW, LH = self.canvas, self.U, self.CW, self.LH
        x0, y0, y1 = self.W - self.RIGHT, self.TOP, self.H - self.BOTTOM
        cv.rect(x0, y0, self.RIGHT, y1 - y0, PANEL)
        cv.rect(x0, y0, 1, y1 - y0, LINE)
        sd = self.side or {}
        kind = sd.get("kind", "text")
        c = EVC.get(kind) or kind_color(kind)
        tx = x0 + 14 * U
        tag = len(kind) * CW + 18 * U
        cv.frame(tx, y0 + 8 * U, tag, 14 * U, 4 * U, mix(c, PANEL, 0.16), mix(c, PANEL, 0.45))
        cv.rrect(tx + 5 * U, y0 + 12 * U, 6 * U, 6 * U, 2 * U, c)
        cv.text(tx + 14 * U, y0 + 11 * U, kind, FG, mix(c, PANEL, 0.16), U)
        cv.text(x0 + self.RIGHT - 22 * U, y0 + 11 * U, "\u00d7", DIM, PANEL, U)
        self.hits.append((x0 + self.RIGHT - 28 * U, y0 + 6 * U, x0 + self.RIGHT - 6 * U, y0 + 24 * U, "side_close", None))
        cols = (self.RIGHT - 28 * U) // CW
        y = y0 + 30 * U
        for l in [w for t_ in plain(sd.get("title", "")).split("\n") for w in wrap(t_, cols)][:3]:
            cv.text(tx, y, l, FG, PANEL, U)
            y += LH
        cv.rect(x0 + 1, y + 2 * U, self.RIGHT - 1, 1, LINE)
        foot = 30 * U
        body_y0, body_y1 = y + 8 * U, y1 - foot
        lines = []
        for kick, key in sd.get("sections", []):
            lines.append(("kick", kick.upper()))
            s = self.seen(*key)
            body = "\u2026" if s is None else (tidy(s, True) or "\u25b8 its handle only")
            lines += [("pre", w) for t_ in plain(body).split("\n") for w in wrap(t_, cols)]
            lines.append(("gap", ""))
        for note in sd.get("notes", []):
            lines += [("note", w) for w in wrap(plain(note), cols)]
        rows_fit = max(1, int((body_y1 - body_y0) // LH))
        self.side_scroll = max(0, min(self.side_scroll, max(0, len(lines) - rows_fit)))
        with self.clip(x0 + 1, body_y0, x0 + self.RIGHT, body_y1):
            for i, (kind_, l) in enumerate(lines[self.side_scroll:self.side_scroll + rows_fit]):
                yy = body_y0 + i * LH
                if kind_ == "kick":
                    cv.text(tx, yy + 2 * U, l, DIM, PANEL, U)
                elif kind_ == "note":
                    cv.text(tx, yy, l, AGENT, PANEL, U)
                elif kind_ == "pre":
                    cv.text(tx, yy, l, FG, PANEL, U)
        cv.rect(x0 + 1, y1 - foot, self.RIGHT - 1, 1, LINE)
        bw = 16 * CW + 12 * U
        cv.frame(tx, y1 - foot + 8 * U, bw, 14 * U, 5 * U, CARD_BG, LINE2)
        cv.text(tx + 6 * U, y1 - foot + 11 * U, "Show in the map", FG, CARD_BG, U)
        self.hits.append((tx, y1 - foot + 8 * U, tx + bw, y1 - foot + 22 * U, "side_map", None))
        self.hits.append((x0, y0, x0 + self.RIGHT, y1, "side_area", None))

    # -- the Context box: the Gantt of context events and the Venn of what the context held -------------------------------

    def short(self, r):
        return re.sub(r"^\S+\s+(?=\S)", "", (r.get("label") or r["id"]) if r else "")

    def what(self, e, i):
        """An event in words: its query, or the crumbs that it put in the context."""
        if e["ev"] == "compress":
            return "the context was compressed"
        if e.get("q"):
            return "\u201c" + e["q"] + "\u201d"
        rs = [self.short(r) for r in self.ctx_data["rows"] if any(b["from"] == i for b in r["bars"])]
        return (", ".join(rs[:2]) + (" and more" if len(rs) > 2 else "")) if rs else ""

    def draw_context(self):
        cv, U, CW = self.canvas, self.U, self.CW
        y0, y1 = self.H - self.MSG - self.CTX_H, self.H - self.MSG
        cv.rect(0, y0, self.W, self.CTX_H, PANEL)
        cv.rect(0, y0, self.W, 1, LINE)
        G = self.ctx_data
        if not G:
            cv.text(14 * U, y0 + 12 * U, "Context: zoom to a session (click a box) to see what the model saw.", DIM, PANEL, U)
            return
        vw = min(VENN_COLS * CW, self.W * 2 // 5)
        gx1 = self.W - vw
        cv.rect(gx1, y0 + 1, 1, self.CTX_H - 1, LINE)
        with self.clip(0, y0 + 1, gx1, y1):
            self.draw_gantt(G, 0, y0 + 1, gx1, y1)
        with self.clip(gx1 + 1, y0 + 1, self.W, y1):
            self.draw_venn(self.venn, gx1 + 1, y0 + 1, self.W, y1)

    def draw_gantt(self, G, x0, y0, x1, y1):
        cv, U, CW, LH = self.canvas, self.U, self.CW, self.LH
        T = max([1, len(G["turns"])] + [e["turn"] + 1 for e in G["events"]])
        LW = 26 * CW
        CWD = max(14 * CW, (x1 - x0 - LW - 2) // T)
        hs = self.gantt_x
        chips_by_turn = {}
        for i, e in enumerate(G["events"]):
            chips_by_turn.setdefault(e["turn"], []).append((i, e))
        # the chips of each turn, wrapped to the column
        placed, max_rows = {}, 1
        for t_, lst in chips_by_turn.items():
            row, rx, cells = 0, 0, []
            for i, e in lst:
                label = plain(e["ev"] + (" (cut)" if e.get("cap") else "") + " " + self.what(e, i))
                w = min(len(label), max(6, (CWD - 12 * U) // CW)) * CW + 10 * U
                if rx and rx + w > CWD - 8 * U:
                    row, rx = row + 1, 0
                cells.append((i, e, row, rx, w, label))
                rx += w + 4 * U
            placed[t_] = cells
            max_rows = max(max_rows, row + 1)
        max_rows = min(max_rows, 3)
        head_h = 8 * U + LH + 2 * LH + max_rows * (LH + 5 * U) + 6 * U
        ry0 = y0 + head_h
        rowh = 16 * U
        self.gantt_scroll = max(0, min(self.gantt_scroll, max(0, len(G["rows"]) * rowh - (y1 - ry0))))
        with self.clip(x0, ry0, x1, y1):
            for k, r in enumerate(G["rows"]):
                ry = ry0 + k * rowh - self.gantt_scroll
                if ry + rowh < ry0 or ry > y1:
                    continue
                cv.rect(x0, ry + rowh - 1, x1 - x0, 1, mix(FG, PANEL, 0.035))
                kc = kind_color(r["kind"])
                cv.text(x0 + 10 * U, ry + (rowh - 8 * U) // 2, r["kind"].upper()[:5], kc, PANEL, U)
                cv.text(x0 + 10 * U + 7 * CW, ry + (rowh - 8 * U) // 2, self.fit(self.short(r), (LW - 10 * U - 7 * CW - 6 * U) // CW), FG, PANEL, U)
                self.hits.append((x0, ry, x1, ry + rowh, "show", (r["id"], 1.0)))
                for e in G["events"]:
                    if e["ev"] == "compress":
                        cv.rect(x0 + LW + e["turn"] * CWD - hs - 1, ry, 2, rowh, mix(EVC["compress"], PANEL, 0.85))
                for b in r["bars"]:
                    xa = x0 + LW + b["t0"] * CWD + 3 * U - hs
                    xb = x0 + LW + (T if b["t1"] is None else b["t1"]) * CWD - 3 * U - hs
                    c = EVC.get(b["ev"]) or kc
                    fill = mix(c, CARD_BG, 0.12 + 0.36 * math.sqrt(max(0.0, min(1.0, b["level"]))))
                    on = self.hi_at is not None and b["from"] == self.hi_at
                    cv.frame(xa, ry + 2 * U, max(6 * U, xb - xa), rowh - 4 * U, 4 * U, fill, FG if on else mix(c, PANEL, 0.7))
                    if xb - xa > 48 * U:
                        s = self.seen(r["id"], b["level"])
                        txt = flat(s) if s else ("\u25b8 " + self.short(r))
                        with self.clip(xa + 3 * U, ry, xb - 3 * U, ry + rowh):
                            cv.text(xa + 6 * U, ry + (rowh - 8 * U) // 2, self.fit(txt or "\u25b8 " + self.short(r), (xb - xa - 12 * U) // CW), FG, fill, U)
                    self.hits.append((xa, ry + 2 * U, max(xa + 6 * U, xb), ry + rowh - 2 * U, "show", (r["id"], b["level"])))
        # the head (it stays when the rows scroll)
        cv.rect(x0, y0, x1 - x0, head_h, PANEL)
        cv.rect(x0, y0 + head_h - 1, x1 - x0, 1, LINE)
        with self.clip(x0, y0, x0 + LW, y0 + head_h):
            cv.text(x0 + 10 * U, y0 + 6 * U, "WHAT THE MODEL SAW", DIM, PANEL, U)
            lx, ly = x0 + 10 * U, y0 + 6 * U + LH + 2 * U
            for ev, c in EVC.items():  # the legend wraps to the width of the corner
                if lx + len(ev) * CW + 14 * U > x0 + LW:
                    lx, ly = x0 + 10 * U, ly + LH
                cv.rrect(lx, ly, 8 * U, 8 * U, 2 * U, c)
                cv.text(lx + 12 * U, ly, ev, DIM, PANEL, U)
                lx += len(ev) * CW + 20 * U
        for t_ in range(T):
            cx = x0 + LW + t_ * CWD - hs
            if cx + CWD < x0 + LW or cx > x1:
                continue
            with self.clip(max(x0 + LW, cx), y0, min(x1, cx + CWD), y0 + head_h):
                cv.rect(cx, y0, 1, head_h, LINE)
                cv.text(cx + 8 * U, y0 + 6 * U, "THE USER SAID", DIM, PANEL, U)
                turn = next((q for q in G["turns"] if q["turn"] == t_), None)
                said = self.short(self.ctx_rows.get(turn["crumb"])) if turn else ""
                if turn:
                    s = self.seen(turn["crumb"], 1)
                    f = flat(s) if s else ""
                    if f:
                        said = "\u201c" + f + "\u201d"
                lines = [w for w in wrap(plain(said), max(6, (CWD - 14 * U) // CW))][:2]
                for j, l in enumerate(lines):
                    cv.text(cx + 8 * U, y0 + 6 * U + LH + j * LH, l, FG, PANEL, U)
                for i, e, row, rx, w, label in placed.get(t_, []):
                    if row >= max_rows:
                        continue
                    c = EVC[e["ev"]]
                    on = i == self.hi_at
                    cy = y0 + 6 * U + 3 * LH + 3 * U + row * (LH + 5 * U)
                    fill = mix(c, CARD_BG, 0.28 if on else 0.11)
                    cv.frame(cx + 8 * U + rx, cy, w, LH + 2 * U, 4 * U, fill, c if on else mix(c, PANEL, 0.38))
                    cv.text(cx + 13 * U + rx, cy + 3 * U, self.fit(label, (w - 10 * U) // CW), FG, fill, U)
                    self.hits.append((cx + 8 * U + rx, cy, cx + 8 * U + rx + w, cy + LH + 2 * U, "chip", i))

    def draw_venn(self, v, x0, y0, x1, y1):
        cv, U, CW, LH = self.canvas, self.U, self.CW, self.LH
        if not v:
            return
        G = self.ctx_data
        e = None if v["at"] is None else G["events"][v["at"]]
        x, y = x0 + 12 * U, y0 + 8 * U - self.venn_scroll
        cols = (x1 - x0 - 24 * U) // CW
        cv.text(x, y, "IN THE CONTEXT AFTER", DIM, PANEL, U)
        y += LH
        if e:
            c = EVC[e["ev"]]
            tw = len(e["ev"]) * CW + 18 * U
            cv.frame(x, y, tw, 14 * U, 4 * U, mix(c, PANEL, 0.16), mix(c, PANEL, 0.45))
            cv.rrect(x + 5 * U, y + 4 * U, 6 * U, 6 * U, 2 * U, c)
            cv.text(x + 14 * U, y + 3 * U, e["ev"], FG, mix(c, PANEL, 0.16), U)
            cv.text(x + tw + 6 * U, y + 3 * U, self.fit(self.what(e, v["at"]), cols - tw // CW - 1), FG, PANEL, U)
        else:
            cv.text(x, y + 3 * U, "no memory events yet", FG, PANEL, U)
        y += 18 * U
        if e and e.get("cap"):
            cv.text(x, y, self.fit("The turn budget cut this %s." % e["ev"], cols), AGENT, PANEL, U)
            y += LH
        sets = {"recalled": "recall", "found": "find", "opened": "open"}
        regs = sorted(v["regions"], key=lambda r: (-len(r["in"]), ",".join(r["in"])))
        for reg in regs:
            ids = reg["ids"]
            rows_ = -(-len(ids) // 2)
            h = 24 * U + rows_ * (3 * LH + 10 * U) + 6 * U
            c = EVC[sets[reg["in"][-1]]]
            frame_fill = mix(c, PANEL, 0.07)
            cv.frame(x - 4 * U, y, x1 - x0 - 16 * U + 8 * U, h, 6 * U, frame_fill, mix(c, PANEL, 0.45))
            names = reg["in"]
            label = names[0] + " only" if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
            hx = x + 2 * U
            for nme in names:
                cv.disc(hx + 4 * U, y + 10 * U, 3.5 * U, EVC[sets[nme]])
                hx += 12 * U
            cv.text(hx + 2 * U, y + 7 * U, self.fit(label.upper(), (x1 - hx - 20 * U) // CW), FG, frame_fill, U)
            cw_ = (x1 - x0 - 24 * U - 6 * U) // 2
            for j, cid in enumerate(ids):
                cx, cy = x + (j % 2) * (cw_ + 6 * U), y + 22 * U + (j // 2) * (3 * LH + 10 * U)
                r = self.ctx_rows.get(cid)
                lv = self.level_at(r, v["at"])
                kind = r["kind"] if r else "text"
                cv.frame(cx, cy, cw_, 3 * LH + 6 * U, 4 * U, CARD_BG, LINE2)
                chip = {"turn": "T", "sess": "S", "span": "#", "tool": "*", "sum": "\u03a3", "mem": "M"}.get(kind, "\u00b6")
                kc = kind_color(kind)
                cv.rrect(cx + 5 * U, cy + 5 * U, 12 * U, 12 * U, 3 * U, kc)
                cv.text(cx + 5 * U + 2 * U, cy + 5 * U + 2 * U, chip, BG, kc, U)
                s = self.seen(cid, lv)
                txt = (flat(s) if s else "") or (self.short(r) if r else cid)
                tcols = max(4, (cw_ - 28 * U) // CW)
                for k, l in enumerate(wrap(plain(txt), tcols)[:3]):
                    cv.text(cx + 22 * U, cy + 5 * U + k * LH, l, FG, CARD_BG, U)
                self.hits.append((cx, cy, cx + cw_, cy + 3 * LH + 6 * U, "show", (cid, lv)))
            y += h + 8 * U
        if e and not regs:
            cv.text(x, y, self.fit("Nothing recalled, found or opened since the last compress.", cols), DIM, PANEL, U)
        self.venn_h = y - (y0 + 8 * U - self.venn_scroll)
        limit = max(0, self.venn_h - (y1 - y0) + 16 * U)
        if self.venn_scroll > limit:
            self.venn_scroll, self.redraw = limit, True

    def level_at(self, r, at):
        """The level of the copy that an event at or before `at` put in, while it is still there."""
        lv = 0.0
        for b in (r["bars"] if r else []):
            if b["from"] is not None and at is not None and b["from"] <= at and (b["to"] is None or b["to"] > at):
                lv = max(lv, b["level"])
        return lv or 1.0

    # -- what the user does: focus, back, find, show, follow ------------------------------------------------------------

    def focus(self, key):
        """Zoomed's rule: the top box again goes back; a box lower in the stack cuts the stack there; a new box is pushed,
        and past 8 boxes the oldest leaves (the trail shows one gap mark for them)."""
        at = self.stack.index(key) if key in self.stack else -1
        if at == len(self.stack) - 1 and at > 0:
            self.stack.pop()
        elif at >= 0:
            del self.stack[at + 1:]
        else:
            self.stack.append(key)
            if len(self.stack) > 9:
                del self.stack[1]
                self.dropped += 1
        self.manual = False
        self.cam.stop_glide()
        n = self.nodes.get(key)
        if n and n.parent is None and n.kind != "text":
            if n.kind == "sess" and n.session:
                self.doc(n)
            else:
                self.parts(n)
        self.redraw = True

    def back(self):
        if len(self.stack) > 1:
            self.stack.pop()
            self.manual = False
            self.cam.stop_glide()
            self.redraw = True

    def home(self):
        self.ctx_pin = ""
        del self.stack[1:]
        self.manual = False
        self.cam.stop_glide()
        self.redraw = True

    def go_trail(self, i):
        del self.stack[i + 1:]
        self.manual = False
        self.cam.stop_glide()
        self.redraw = True

    def set_layout(self, k):
        self.layout = k if k in dict(LAYOUTS) else "grid"
        self.dirty, self.temp, self.manual = True, max(self.temp, 0.3), False
        self.redraw = True

    def glow_node(self, n, seconds):
        n.glow = self.clock() + seconds
        self.redraw = True

    def reveal_job(self, ids):
        """Open the boxes along a path (session first) and zoom to the last."""
        n = self.nodes.get(ids[0])
        if not n:
            n = self.add_root(self.view.graph(root=ids[0])["nodes"][0])
        if n.kind == "sess" and n.session:  # a session is a document: zoom to the card of the deepest crumb
            self.doc(n)
            while n.doc is None or n.doc.state == "loading":
                yield WAIT
            k = None
            if n.doc.state == "ready":
                for i in range(len(ids) - 1, 0, -1):
                    k = n.doc.by_crumb.get(ids[i])
                    if k:
                        break
            c = self.nodes.get(k) if k else None
            if c:
                self.glow_node(c, 5.0)
                if n.key not in self.stack:
                    self.stack[:] = ["*", n.key]
                if self.stack[-1] != c.key:
                    self.focus(c.key)
                else:
                    self.manual = False
                return
        for cid in ids[1:]:
            self.parts(n)
            while n.kids is None:
                yield WAIT
            nxt = next((self.nodes[kk] for kk in n.kids if self.nodes[kk].id == cid), None)
            if not nxt:
                break
            n = nxt
        self.glow_node(n, 5.0)
        if self.stack[-1] != n.key:
            self.focus(n.key)

    def reveal_id_job(self, cid):
        ids = self.view.path(cid)
        yield
        yield from self.reveal_job(ids or [cid])

    def search_job(self, q):
        groups = self.view.find(q, 12)
        for gr in groups:
            a = gr.get("session") and self.nodes.get(gr["session"])
            if a:
                self.glow_node(a, 8.0)
        self.say(("%d sessions hold hits; the first one is open" % len(groups)) if groups else "no match")
        first = next((g for g in groups if g.get("session")), None)
        if first:
            yield
            ids = self.view.path(first["hits"][0]["id"])
            yield from self.reveal_job(ids or [first["session"]])

    def link_job(self, n, ref):
        """A handle line: zoom to that part (its box is in the grid under the text)."""
        self.parts(n)
        while n.kids is None:
            yield WAIT
        kids = [self.nodes[k] for k in n.kids]
        base = ref.split(":")[0]
        k = next((k for k in kids if k.id == ref), None) or next((k for k in kids if k.id.split(":")[0] == base), None)
        self.focus(k.key if k else n.key)

    def follow_tick(self):
        try:
            tr = self.view.trail()
        except (OSError, ValueError, KeyError):
            return
        st = tr.get("stack") or []
        line = ("\u2026 \u203a " if tr.get("dropped") else "") + " \u203a ".join(e["label"] for e in st)
        self.agent_line = line if st else ""
        self.agent_ids = [e["id"] for e in st]
        if tr.get("session"):
            self.ctx_sid = tr["session"]
        top = st[-1]["id"] if st else ""
        self.redraw = True
        if not self.follow_on or not top or top + line == self.last_agent:
            return
        self.last_agent = top + line
        within = st[-2]["id"] if len(st) > 1 else ""

        def go():
            ids = self.view.path(top)
            if not ids and within:
                ids = self.view.path(top, within=within)
            yield
            yield from self.reveal_job(ids or [top])
        self.spawn(go())

    def selected_sid(self):
        """The session of the box that the map is zoomed to."""
        sid = self.ctx_pin or self.ctx_sid
        if not self.ctx_pin:
            n = self.nodes.get(self.stack[-1])
            while n:
                if n.kind == "sess" and n.session:
                    sid = n.session
                n = self.nodes.get(n.parent) if n.parent is not None else None
        return sid

    def context_job(self):
        sid = self.selected_sid()
        if not sid:
            return
        if sid != self.ctx_shown:
            self.ctx_shown, self.ctx_at, self.ctx_key = sid, None, None
        try:
            G = self.view.context(sid)
            v = self.view.venn(sid, self.ctx_at) if self.ctx_at is not None else self.view.venn(sid)
        except KeyError:
            return
        key = (sid, len(G["events"]), len(G["rows"]), self.W, self.ctx_at)
        if key != self.ctx_key:
            self.ctx_key, self.ctx_data, self.ctx_rows, self.hi_at, self.venn = key, G, {r["id"]: r for r in G["rows"]}, v["at"], v
            self.redraw = True
        yield

    def show(self, cid, level=1.0, zoom=True):
        """The text of a crumb in the side panel: what the model saw (at its level), and its whole text."""
        r = self.ctx_rows.get(cid)
        n = next((x for x in self.nodes.values() if x.id == cid), None)
        kind = r["kind"] if r else n.kind if n else "text"
        title = self.short(r) if r else (n.trail if n.in_doc else name(n)[len(kind) + 1:]) if n else cid
        secs = ([("What the model saw", (cid, level))] if level < 1 else []) + [("The whole crumb" if level < 1 else "The crumb", (cid, 1.0))]
        self.side = {"kind": kind, "title": title, "sections": secs, "notes": [], "map": cid}
        self.open_side()
        if zoom:
            self.ctx_pin = self.ctx_shown
            self.spawn(self.reveal_id_job(cid))

    def show_event(self, e, i):
        """An event: the exact context that it gave the model (its receipt at level 1)."""
        notes = ["The turn budget cut this %s: the model got the first part and a handle to the rest." % e["ev"]] if e.get("cap") else []
        first = next((r for r in self.ctx_data["rows"] if any(b["from"] == i for b in r["bars"])), None)
        self.side = {"kind": e["ev"], "title": self.what(e, i),
                     "sections": [("The compress" if e["ev"] == "compress" else "What it put in the context", (e["id"], 1.0))],
                     "notes": notes, "map": first["id"] if first else None}
        self.open_side()

    def open_side(self):
        self.side_on, self.side_scroll = True, 0
        self.resize_panels()

    def close_side(self):
        self.side_on = False
        self.resize_panels()

    def toggle_context(self):
        self.ctx_on = not self.ctx_on
        self.ctx_key = None
        self.resize_panels()
        self.timers["context"] = 0.0

    # -- what the pointer hits ---------------------------------------------------------------------------------------

    def at(self, x, y):
        """The link or the box at a point of the map: ("link", link) or ("node", node), or None."""
        for l in self.links:
            if l[0] <= x <= l[0] + l[2] and l[1] <= y <= l[1] + l[3]:
                return ("link", l)
        best = None
        for b in self.boxes:
            if b[1] <= x <= b[1] + b[3] and b[2] <= y <= b[2] + b[4] and (best is None or b[5] >= best[5]):
                best = b
        return ("node", best[0]) if best else None

    def ui_at(self, x, y):
        for h in reversed(self.hits):
            if h[0] <= x < h[2] and h[1] <= y < h[3]:
                return h
        return None

    def in_map(self, x, y):
        return self.TOP <= y < self.H - self.BOTTOM and 0 <= x < self.W - self.RIGHT

    def do_action(self, action, arg):
        if action == "home":
            self.home()
        elif action == "context":
            self.toggle_context()
        elif action == "follow":
            self.follow_on = not self.follow_on
            self.last_agent = ""
            self.timers["follow"] = 0.0
        elif action == "layout":
            self.set_layout(arg)
        elif action == "find":
            self.find_on = True
        elif action == "trail":
            self.go_trail(arg)
        elif action == "side_close":
            self.close_side()
        elif action == "side_map":
            if self.side and self.side.get("map"):
                self.ctx_pin = self.ctx_shown
                self.spawn(self.reveal_id_job(self.side["map"]))
        elif action == "show":
            self.show(*arg)
        elif action == "chip":
            self.ctx_at, self.ctx_key = arg, None
            self.timers["context"] = 0.0
            self.show_event(self.ctx_data["events"][arg], arg)
        self.redraw = True

    def click(self, x, y):
        """A click on the map: a handle line zooms to its part; a box is focused (the same box again goes back)."""
        hit = self.at(x, y)
        if hit and hit[0] == "link":
            self.spawn(self.link_job(hit[1][5], hit[1][4]))
        elif hit:
            n = hit[1]
            self.ctx_pin = ""
            self.focus(n.key)
            if self.side_on and not getattr(n, "synthetic", False):
                self.show(n.id, 1.0, False)

    def near(self, dx, dy):
        """Zoomed's "zoom near": the closest box of the same depth in that direction."""
        cur = self.nodes.get(self.stack[-1])
        cx, cy = (self.sx(cur.x), self.sy(cur.y)) if cur else (self.mid_x(), self.mid_y())
        cb = next((b for b in self.boxes if b[0] is cur), None)
        want, best, bd = (cb[5] if cb else 0), None, float("inf")
        for b in self.boxes:
            ex, ey = b[1] + b[3] / 2 - cx, b[2] + b[4] / 2 - cy
            if b[0] is cur or ex * dx + ey * dy <= 4 or b[5] != want:
                continue
            d = math.hypot(ex, ey) + 2 * abs(ex * dy - ey * dx)
            if d < bd:
                bd, best = d, b[0]
        if best:
            self.focus(best.key)

    # -- events ------------------------------------------------------------------------------------------------------

    def handle(self, ev):
        """One event from the terminal."""
        self.redraw = True
        if ev.kind == "mode":
            if ev.name == "1016" and os.environ.get("BREADCRUMBS_MOUSE") != "cells":
                self.pixel_mouse = ev.value in (1, 3) and self.xpix > 0
        elif ev.kind == "mouse":
            self.mouse(ev)
        elif ev.kind in ("key", "char"):
            self.key(ev)

    def to_img(self, ev):
        """A pointer position of the terminal in picture pixels: SGR pixels if the terminal has them, else the middle of the cell."""
        if self.pixel_mouse and self.xpix > 0 and self.ypix > 0:
            return ev.x * self.W / self.xpix, ev.y * self.H / self.ypix
        return (ev.x - 0.5) * self.W / self.cols, (ev.y - 0.5) * self.H / self.rows

    def mouse(self, ev):
        x, y = self.to_img(ev)
        now, a = self.clock(), ev.action
        if a == "wheel":
            if ev.dy == 0:
                return
            if self.ctx_on and y >= self.H - self.MSG - self.CTX_H and y < self.H - self.MSG:
                if x >= self.W - min(VENN_COLS * self.CW, self.W * 2 // 5):
                    self.venn_scroll = max(0, self.venn_scroll - ev.dy * 3 * self.LH)
                elif ev.mods & SHIFT:
                    self.gantt_x = max(0, self.gantt_x - ev.dy * 3 * self.CW)
                else:
                    self.gantt_scroll = max(0, self.gantt_scroll - ev.dy * 2 * 16 * self.U)
            elif self.side_on and x >= self.W - self.RIGHT and self.TOP <= y < self.H - self.BOTTOM:
                self.side_scroll = max(0, self.side_scroll - ev.dy * 3)
            elif self.in_map(x, y):
                self.manual = True
                self.cam.zoom_at(x, y, ev.dy, self.mid_x(), self.mid_y())
            return
        if a == "press" and ev.button == 0:
            h = self.ui_at(x, y)
            if not (h and h[4] == "find"):
                self.find_on = False
            if h:
                self.drag = None
                return self.do_action(h[4], h[5])
            if self.in_map(x, y):
                self.cam.grab()   # a press stops a glide
                self.drag = Drag(x, y, self.cam.x, self.cam.y, now)
            return
        if a == "move":
            if self.drag and ev.button == 0:
                dx, dy = x - self.drag.x, y - self.drag.y
                if abs(dx) + abs(dy) > 4 * self.U:
                    self.drag.moved = True
                if self.drag.moved:
                    self.manual = True
                    self.cam.drag(self.drag.cx - dx / self.cam.k, self.drag.cy - dy / self.cam.k)
                    self.drag.add(now, x, y)
                return
            hit = self.at(x, y) if self.in_map(x, y) else None
            h = hit[1] if hit and hit[0] == "node" else None
            self.hover_xy = (x, y)
            if h is not self.hover:
                self.hover = h
            return
        if a == "release" and ev.button == 0:
            d, self.drag = self.drag, None
            if not d:
                return
            if not d.moved:
                self.click(x, y)
            elif self.motion:
                vx, vy = d.speed(now)
                sp = math.hypot(vx, vy)
                if sp >= FLICK:
                    if sp > SPEED_MAX:
                        vx, vy = vx * SPEED_MAX / sp, vy * SPEED_MAX / sp
                    self.cam.throw(-vx / self.cam.k, -vy / self.cam.k)

    def key(self, ev):
        name = ev.name or ev.text
        if self.find_on:
            if name == "enter":
                self.find_on = False
                if self.query.strip():
                    self.spawn(self.search_job(self.query.strip()))
            elif name == "esc":
                self.find_on = False
            elif name == "backspace":
                self.query = self.query[:-1]
            elif name == "ctrl-u":
                self.query = ""
            elif ev.kind in ("char", "key") and ev.text and not ev.mods & CTRL:
                self.query += ev.text
            return
        shift, step = bool(ev.mods & SHIFT), 120 * self.U
        if name in ("q", "ctrl-c"):
            self.running = False
        elif name == "esc":
            if self.side_on:
                self.close_side()
            else:
                self.back()
        elif name == "backspace":
            self.back()
        elif name in ("up", "down", "left", "right"):
            d = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}[name]
            if shift:
                self.manual = True
                self.cam.pan_by(d[0] * step / self.cam.k, d[1] * step / self.cam.k)
            else:
                self.near(*d)
        elif name in ("w", "a", "s", "d"):
            d = {"w": (0, -1), "s": (0, 1), "a": (-1, 0), "d": (1, 0)}[name]
            self.manual = True
            self.cam.pan_by(d[0] * step / self.cam.k, d[1] * step / self.cam.k)
        elif name in ("+", "="):
            self.manual = True
            self.cam.zoom_at(self.mid_x(), self.mid_y(), 1, self.mid_x(), self.mid_y())
        elif name == "-":
            self.manual = True
            self.cam.zoom_at(self.mid_x(), self.mid_y(), -1, self.mid_x(), self.mid_y())
        elif name in ("0", "1", "home"):
            self.home()
        elif name == "2":
            self.toggle_context()
        elif name == "3":
            self.do_action("follow", None)
        elif name == "4":
            ks = [k for k, _ in LAYOUTS]
            self.set_layout(ks[(ks.index(self.layout) + 1) % len(ks)])
        elif name == "/":
            self.find_on = True
            self.query = ""
        elif name in ("enter", "space"):
            n = self.nodes.get(self.stack[-1])
            if n and not n.synthetic:
                self.show(n.id, 1.0, False)
        elif name == "pgup" or name == "pgdn":
            if self.side_on:
                self.side_scroll = max(0, self.side_scroll + (8 if name == "pgdn" else -8))

    # -- the frame loop ------------------------------------------------------------------------------------------------

    def aspect(self):
        return self.W / max(1, self.H - self.TOP - self.MSG)

    def aim(self, x0, y0, x1, y1):
        k = min((self.W - self.RIGHT - 30 * self.U) / max(1e-3, x1 - x0), (self.H - self.TOP - self.BOTTOM - 16 * self.U) / max(1e-3, y1 - y0))
        self.cam.aim((x0 + x1) / 2, (y0 + y1) / 2, max(K_MIN, min(K_MAX, k)))

    def aim_top(self):
        n = self.nodes.get(self.stack[-1])
        if n and n.in_doc:
            s = self.nodes.get(n.sess)
            if s and s.doc and s.doc.state == "ready":
                self.doc_place(s, s.doc)
        if n:
            hw, hh = (n.w or n.s) * 0.52, (n.h or n.s) * 0.52
            return self.aim(n.x - hw, n.y - hh, n.x + hw, n.y + hh)
        if self.roots:
            x0 = min(a.x - a.w / 2 for a in self.roots)
            y0 = min(a.y - a.h / 2 for a in self.roots)
            x1 = max(a.x + a.w / 2 for a in self.roots)
            y1 = max(a.y + a.h / 2 + 16 * self.U / self.cam.k for a in self.roots)
            self.aim(x0 - 8, y0 - 8, x1 + 8, y1 + 8)

    def layout_moving(self):
        if self.layout == "force":
            return self.temp >= 0.004
        if self.dirty:
            return True
        for a in self.roots:
            if abs(a.tx - a.x) > 0.02 or abs(a.ty - a.y) > 0.02 or abs((a.tw or a.s) - a.w) > 0.02 or abs((a.th or a.s) - a.h) > 0.02:
                return True
        return False

    def animating(self):
        return bool(self.cam.moving() or self.jobs or self.layout_moving())

    def advance(self, dt, first=False):
        """Step the world by dt seconds (layout, camera, jobs, timers). dt is the real time between two calls."""
        now = self.clock()
        if self.layout == "force":
            self.temp = force_step(self.roots, self.edges, self.temp, self.rng)
        elif self.dirty:
            arrange(self.roots, self.edges, self.layout, self.aspect())
            self.dirty = False
        place(self.roots, self.layout, 1 - math.exp(-dt * 7))
        if not self.manual:
            self.aim_top()
        if first and self.roots:
            self.cam.snap()
        was = self.cam.moving()
        self.cam.step(dt, self.mid_x(), self.mid_y())
        if was or self.cam.moving():
            self.redraw = True
        if self.layout_moving():
            self.redraw = True
        self.pump()
        if self.view is not None:
            if now >= self.timers["follow"]:
                self.timers["follow"] = now + 2.0
                self.follow_tick()
            if self.ctx_on and now >= self.timers["context"]:
                self.timers["context"] = now + 2.0
                self.spawn(self.context_job())
        glows = [n.glow for n in self.nodes.values() if n.glow]
        if glows and any(g - 0.05 <= now <= g + 0.12 for g in glows):
            self.redraw = True

    def settle(self, seconds=20.0, dt=1.0 / 30):
        """Advance with a fixed dt until nothing moves and every job is done (headless). It paints each step, because
        drawing is what asks for the texts of the boxes."""
        first = self.frames == 0 and self.cam.k == 0.5
        for i in range(int(seconds / dt)):
            self.advance(dt, first=first and i == 0)
            self.paint()
            self.drain()
            if not self.animating():
                self.paint()
                self.drain()
                if not self.animating() and not self.jobs:
                    break
        self.paint()
        return self

    def present(self):
        """Draw a frame and send it, unless its pixels are those of the last one. Returns True if it was sent."""
        cv = self.paint()
        self.frames += 1
        self.redraw = False
        if self.last_buf is not None and cv.buf == self.last_buf:
            self.skipped += 1
            return False
        self.last_buf = bytes(cv.buf)
        self.write(kg_frame(cv.png(1), self.cols, self.rows))
        self.sent += 1
        return True

    def write(self, s):
        """Send text to the terminal. If the terminal is gone (the window was closed: EIO or EPIPE), stop quietly."""
        if self.gone:
            return
        out = self.out or sys.stdout
        try:
            out.write(s)
            out.flush()
        except (OSError, ValueError):
            self.gone, self.running = True, False
            if self.out is None:  # the text that was not sent must not make Python fail again when it flushes stdout at exit
                try:
                    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                except (OSError, ValueError):
                    pass

    # -- the terminal --------------------------------------------------------------------------------------------------

    def read_size(self):
        cols, rows, xp, yp = term_size(sys.stdout.fileno())
        self.resize(cols, rows, xp, yp)

    def open_terminal(self):
        self.fd_in = sys.stdin.fileno()
        self.old_termios = termios.tcgetattr(self.fd_in)
        self.old_handlers = {}
        for sig, fn in ((signal.SIGTERM, self.on_stop), (signal.SIGHUP, self.on_stop), (signal.SIGINT, self.on_stop),
                        (getattr(signal, "SIGWINCH", None), self.on_winch)):
            if sig is not None:
                self.old_handlers[sig] = signal.signal(sig, fn)
        self.opened = True
        tty.setraw(self.fd_in)
        self.write(enter_codes(os.environ.get("BREADCRUMBS_HOVER", "1") != "0"))

    def close_terminal(self):
        """Every way out ends here: the image is deleted, the mouse modes are off, the cursor is back, the main screen again."""
        if not self.opened:
            return
        self.opened = False
        try:
            self.write(leave_codes())
        finally:
            if self.old_termios is not None:
                try:
                    termios.tcsetattr(self.fd_in, termios.TCSAFLUSH, self.old_termios)
                except (termios.error, OSError):  # the terminal is gone: there is nothing to restore
                    pass
            for sig, h in (self.old_handlers or {}).items():
                signal.signal(sig, h)

    def on_stop(self, signum, frame):
        self.running = False

    def on_winch(self, signum, frame):
        self.winch = True

    winch = False
    gone = False   # the terminal is gone (the window was closed): nothing more is written

    def run(self, max_frames=0):
        """Take the terminal and show the explorer until q, Ctrl-C, SIGTERM or SIGHUP. max_frames > 0: stop after that many frames."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()) or termios is None:
            print("breadcrumbs spacegraph: needs a terminal that reads the Kitty graphics protocol (Kitty, Ghostty)", file=sys.stderr)
            raise SystemExit(1)
        if not kitty.supported():
            print("breadcrumbs spacegraph: this terminal may not read the Kitty graphics protocol (Kitty, Ghostty)", file=sys.stderr)
        try:
            self.init_view()
        except FileNotFoundError:
            print("breadcrumbs spacegraph: no store at %s" % self.root, file=sys.stderr)
            raise SystemExit(1)
        try:
            self.read_size()
            self.open_terminal()
            self.loop(max_frames)
        finally:
            self.close_terminal()
            self.view.close()
        return 0

    def loop(self, max_frames=0):
        inp, fps = Input(), max(1.0, self.fps)
        self.running, last, due, first = True, self.clock(), 0.0, True
        sent = 0
        while self.running and not self.gone:
            now = self.clock()
            dt, last = max(0.0, min(0.5, now - last)), now
            if self.winch:
                self.winch = False
                self.read_size()
            self.advance(dt, first=first)
            first = False
            if (self.redraw or self.animating()) and now >= due:
                self.present()
                due = max(due + 1.0 / fps, now)   # a steady cadence; if the frame took longer, the next one is due at once
                sent += 1
                if max_frames and sent >= max_frames:
                    break
            busy = self.redraw or self.animating()
            timeout = max(0.0, due - self.clock()) if busy else 0.25
            if inp.pending():
                timeout = min(timeout, 0.03)
            try:
                ready = select.select([self.fd_in], [], [], timeout)[0]
            except InterruptedError:
                continue
            except (OSError, ValueError):  # the terminal is gone
                break
            if ready:
                try:
                    data = os.read(self.fd_in, 65536)
                except OSError:  # EIO: the terminal is gone
                    break
                if not data:
                    break
                evs = inp.feed(data)
            elif inp.pending():
                evs = inp.flush()
            else:
                evs = []
            for ev in evs:
                self.handle(ev)


class _Clip:
    """with canvas clip: the clip rectangle becomes the overlap of the old one and a new one, and comes back at the end."""

    def __init__(self, cv, x0, y0, x1, y1):
        self.cv, self.new, self.old = cv, (x0, y0, x1, y1), None

    def __enter__(self):
        cv = self.cv
        self.old = cv.clip
        ox0, oy0, ox1, oy1 = self.old
        x0, y0, x1, y1 = (round(max(-1e7, min(1e7, v))) for v in self.new)
        x0, y0, x1, y1 = max(ox0, x0), max(oy0, y0), min(ox1, x1), min(oy1, y1)
        cv.clip = (x0, y0, max(x0, x1), max(y0, y1))

    def __exit__(self, *exc):
        self.cv.clip = self.old


def term_size(fd):
    """(columns, rows, width in pixels, height in pixels) of the terminal on fd; the pixels are 0 where the terminal does not say."""
    try:
        import fcntl
        rows, cols, xp, yp = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
        if cols and rows:
            return cols, rows, xp, yp
    except (ImportError, OSError, AttributeError):
        pass
    import shutil
    size = shutil.get_terminal_size((100, 30))
    return size.columns, size.lines, 0, 0


def main(argv=None):
    """python3 -m breadcrumbs.spacegraph_kitty STORE [--motion 0] [--snapshot FILE.png]"""
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m breadcrumbs.spacegraph_kitty", description="The explorer page as a native Kitty/Ghostty program.")
    ap.add_argument("root", help="the store folder (<hermes home>/breadcrumbs)")
    ap.add_argument("--motion", choices=("0", "1"), default=None, help="0: the camera of the old page (no momentum)")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames (0: until q)")
    ap.add_argument("--snapshot", metavar="PNG", help="write one settled frame as a PNG and exit (no terminal needed)")
    ap.add_argument("--size", default="1280x720", help="the picture size of --snapshot")
    ap.add_argument("--layout", choices=[k for k, _ in LAYOUTS], help="--snapshot: the layout")
    ap.add_argument("--focus", help="--snapshot: zoom to this crumb id first")
    ap.add_argument("--context", action="store_true", help="--snapshot: with the Context box")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.root):
        print("breadcrumbs spacegraph: %s is not a folder" % args.root, file=sys.stderr)
        return 1
    app = SpaceGraphKitty(args.root, motion=args.motion)
    if not args.snapshot:
        app.run(args.frames)
        return 0
    try:
        w, h = (int(v) for v in args.size.lower().split("x"))
        app.headless(max(10, w // 9), max(4, h // 18), w, h)
    except FileNotFoundError:
        print("breadcrumbs spacegraph: no store at %s" % args.root, file=sys.stderr)
        return 1
    try:
        if args.layout:
            app.set_layout(args.layout)
        if args.context:
            app.toggle_context()
        app.settle()
        if args.focus:
            app.spawn(app.reveal_id_job(args.focus))
            app.settle()
        with open(args.snapshot, "wb") as f:
            f.write(app.paint().png())
    finally:
        app.view.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
