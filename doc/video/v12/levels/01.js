// v12 level 1, `spine` (task F11). Plan section 5, level 1: "the lines of one text; the list of their ids is the text".
// The one caption is  text = crc32c(spine).
//
// In the frame: the 157 lines of the hero text of the demo store (store.json: spine.lines, and spine.ids for the id of each
// line), in three columns. A line is its id and its text (a line of up to 62 characters shows whole; a longer one shows its
// first 61 and a "…"). A line id is
// YELLOW, as the address of levels/00.js is; the id of the whole text is TEAL, this level's colour (levels/02.js draws it
// TEAL in its ring too).
// The camera comes from level 0, the hero line (line 3 of the text, id 2b5a7671), and pulls back: the page opens round it.
// Then one teal stroke runs down the ids of each column, in order. That is the spine. Each id lights as the stroke passes it,
// and the text behind it steps back. The last thing drawn is the id of the whole text, fd3130cf (spine.crc32c), written in
// a hole of the page. No made-up text: each string drawn is an id of spine.ids, a part of one line of spine.lines, or
// spine.crc32c. The page lies inside the disc R (the file throws if it does not).
//
// DATA is store.json's `spine` (the sha256 of that store.json is in DATA.source). To rebuild its line, from v12/:
//   python3.14 -c "import json;d=json.load(open('store.json'));s=d['spine'];print('  const DATA = '+json.dumps({'source':'store.json sha256 2c5444e37dd98ca8c1c2e17257dcd6e7ceecf03027fa42ddc7662e6db7caae62','crc':s['crc32c'],'hero':d['line']['index'],'ids':s['ids'],'lines':[t.rstrip('\n') for t in s['lines']]},ensure_ascii=False,separators=(',',':'))+';')"
//
// For the join (F30). Level 0 sits in this frame through place(), tuned on the real levels/00.js (F10, as it was at 18:33): its row
// of 81 bytes (1.944 units wide, a byte P = 1.944 / 81 = 0.024 wide, centred at x 0, at y 0) is laid on the hero row of the page, byte on
// character: K0 = CW / P is the size of level 0 in this frame (0.5), so the camera pulls back by about 3 from level 0's
// view to this page, and the line is one row of the page when the page opens round it. BELOW holds the three numbers of
// levels/00.js that this needs; if that file changes its row, change BELOW. R is 1.6, as in stubs/01.js, so a placement of
// this level that was tuned on the stub (levels/02.js: BELOW_R = 1.6) holds.
(function (F) {
  'use strict';
  const { C, clamp, lerp, seg, rrectPts, at } = F.kit;

  // DATA-BEGIN
  const DATA = {"source":"store.json sha256 2c5444e37dd98ca8c1c2e17257dcd6e7ceecf03027fa42ddc7662e6db7caae62","crc":"fd3130cf","hero":2,"ids":["2d3549e5","b9c82932","2b5a7671","ddb6b4e1","cd384d5b","a8ed5938","a2217323","d9c704ca","9eeef670","2b3564fd","e6fa4679","cd0276ee","d78e5cff","6c7ff704","b610bc47","4d83326c","07de1637","aede99b5","a0b03037","04b75184","fba7b763","89d9b6b0","4e4ad009","4e8935ee","641d23d1","9b304fec","04c1706a","556c5ad4","30fd256c","25208cdc","b594351d","0d34f1f3","68e97f7e","8160aa3f","f75109f3","5376491a","c8807837","78825825","99e66652","6b5d47b9","3e8695b9","85428f2d","f6298e8f","48d3b229","a54c4b1e","4a76f1d0","3e80752f","e3b53811","a721ddf3","7372b06b","50d0b83a","4bc48b49","64a407bd","d85c0965","7d1d9b1b","c31ba0ec","e66da70d","f3576f37","5d2b4930","0150ecb6","81e1b638","6bcf764b","76e71da1","e35411ce","1f3f1222","f511d251","77c06c5b","1f2e47eb","2cbdf2d6","f49855e6","f5a49559","6d586f24","a6e8597b","b145a05b","c27ab89a","285478e9","5e6f3c52","80c2930b","b06259df","56bddb74","7509db70","4011d49e","63018077","b1dc59b7","2d3e63b6","c710a3c5","df2f236f","d7ac186f","85e989dd","8c5356f4","d5b2d910","e82c3c66","b1ff06ca","eaff7034","bfe952a2","4e582830","5bee04e3","b14f2302","9c189220","ae97f4fe","5ad5fb5b","07ba85bf","bf288ea1","a9226dd0","60d2a948","c15b4df4","613166ab","de8c6a0a","c9cb6965","b851efa6","8239ba0d","eefb3920","1a753aff","8c8343d7","4e9dbd73","b2746308","63e267a5","0b50fb17","033e0a96","fe0c936f","96ea46c9","c6e6fdb5","d1c644b0","18177637","d2901639","a7bb1cfa","ec9e0f6a","8dfb5b8e","96a803e5","6a850a43","2250117d","b2218e86","b7390e2a","bd979dcd","005fee3e","c07217b3","0a6a69e3","25752495","3dfd8aac","88b0dd11","a2d39fa1","59d00616","c1e82211","a4972507","f0f73ecf","7e28dfc6","9df9f7c0","f3d427b6","99b16b31","321fab7a","0ccf3e5b","e437b622","7abec82e","9231b5eb","1f4c9330","281a8c8a","70c0957d"],"lines":["1|\"\"\"Immutable content-addressed blob store.","2|","3|Address = CRC32C of the bytes, as 8 hex digits. A different blob with the same","4|CRC32C gets an ordinal suffix: \"10e89673.1\". Blobs are never changed or deleted.","5|","6|Files in the store directory:","7|  blobs.pack  append-only compressed blocks:","8|              header \"<2sBBIII\": b\"BC\", codec, 0, raw_len, comp_len, crc32c(payload)","9|  blobs.idx   append-only 24-byte records \"<IHHQII\":","10|              crc, ordinal, flags, block_offset, offset_in_block, length","11|A block is written before its index records, so the index never points at","12|missing bytes. Every read checks the block CRC and the blob CRC.","13|","14|The open index makes no Python object per blob. Row p is item p of four arrays: crc, block,","15|offset, length (16 bytes; the block array widens to 8 bytes when a block starts past 4 GiB).","16|Rows are in the order this Store read or put them. A slot table of p + 1 finds the ordinal-0","17|row of a crc: open addressing on crc & mask, linear probing, at most 3/4 full (it doubles).","18|The rows of ordinals 1, 2, ... (CRC32C collisions, rare) are in a small dict.","19|","20|Two processes can write one store (the Hermes gateway and the CLI). Each flush holds an","21|exclusive lock on the file \"lock\" and first reads the index records that other writers","22|appended. An id that names other bytes in another writer fails loudly. A torn record from a","23|crashed writer is cut off. Without fcntl (Windows) use one writer per store.","24|\"\"\"","25|","26|import contextlib","27|import os","28|import struct","29|import sys","30|import zlib","31|from array import array","32|from collections import OrderedDict","33|","34|from .crc32c import crc32c","35|from .runtime import no_gc","36|","37|try:","38|    import fcntl","39|except ImportError:  # pragma: no cover - no flock on this platform","40|    fcntl = None","41|","42|try:  # CPython 3.14+ has zstd in the standard library.","43|    from compression import zstd as _zstd  # type: ignore","44|except Exception:  # pragma: no cover - depends on the runtime","45|    _zstd = None","46|","47|BLOCK_SIZE = 64 * 1024","48|RAW, ZLIB, ZSTD = 0, 1, 2","49|_HDR = struct.Struct(\"<2sBBIII\")","50|_REC = struct.Struct(\"<IHHQII\")","51|_SLOTS = 1024  # first size of the slot table (a power of 2)","52|_READ = 1024 * _REC.size  # index bytes per read: a small buffer does not stay in RSS after use","53|_BIG_ENDIAN = sys.byteorder == \"big\"  # the files are little-endian","54|","55|","56|def parse_id(blob_id):","57|    head, _, ordinal = blob_id.partition(\".\")","58|    if len(head) != 8:","59|        raise KeyError(blob_id)","60|    return int(head, 16), int(ordinal) if ordinal else 0","61|","62|","63|def format_id(crc, ordinal):","64|    return \"%08x\" % crc if ordinal == 0 else \"%08x.%d\" % (crc, ordinal)","65|","66|","67|def _compress(raw, codec):","68|    if codec == ZSTD and _zstd is not None:","69|        comp = _zstd.compress(raw)","70|    elif codec in (ZLIB, ZSTD):","71|        codec, comp = ZLIB, zlib.compress(raw, 6)","72|    else:","73|        return RAW, raw","74|    return (codec, comp) if len(comp) < len(raw) else (RAW, raw)","75|","76|","77|def _decompress(comp, codec):","78|    if codec == RAW:","79|        return comp","80|    if codec == ZLIB:","81|        return zlib.decompress(comp)","82|    if codec == ZSTD and _zstd is not None:","83|        return _zstd.decompress(comp)","84|    raise ValueError(\"codec %d is not available in this runtime\" % codec)","85|","86|","87|class Store:","88|    def __init__(self, path, codec=ZLIB, cache_blocks=32, readonly=False):","89|        \"\"\"readonly: open an existing store to read; create and change no file, refuse puts.\"\"\"","90|        if readonly:","91|            if not os.path.isdir(path):","92|                raise FileNotFoundError(path)","93|        else:","94|            os.makedirs(path, exist_ok=True)","95|        self.path, self.readonly = path, readonly","96|        self.codec = codec","97|        self._crc = array(\"I\")  # the index: one item per row (see the module doc)","98|        self._blk = array(\"I\")","99|        self._off = array(\"I\")","100|        self._len = array(\"I\")","101|        self._slots = array(\"I\", [0]) * _SLOTS  # row + 1 of the ordinal-0 blob of a crc; 0 = empty","102|        self._used = 0  # filled slots","103|        self._more = {}  # crc -> [row of ordinal 1, row of ordinal 2, ...]","104|        self._dead = set()  # rows of index records that add no blob (see _catch_up)","105|        self._buf = bytearray()","106|        self._pending = {}  # row -> ordinal, in put order, for blobs in the open block (offset in _buf)","107|        self._cache = OrderedDict()","108|        self._cache_blocks = cache_blocks","109|        self._idx_path = os.path.join(path, \"blobs.idx\")","110|        self._idx_size = 0  # bytes of blobs.idx read so far (whole records)","111|        self._clashes = []  # (row, block, off, length): another writer's record on one of our pending ids","112|        self._pack = self._idx = self._lockf = None","113|        if not readonly:","114|            self._pack = open(os.path.join(path, \"blobs.pack\"), \"ab\")","115|            self._idx = open(self._idx_path, \"ab\")","116|            self._lockf = open(os.path.join(path, \"lock\"), \"ab\")","117|        self._reader = open(os.path.join(path, \"blobs.pack\"), \"rb\")","118|        with no_gc():  # a big load makes many short-lived tuples: skip the cyclic GC scans","119|            self._catch_up()","120|","121|    # -- the index --------------------------------------------------------","122|    def _slot(self, crc):","123|        \"\"\"Row of the ordinal-0 blob of crc, or ~slot (below 0) of the empty slot where it goes.\"\"\"","124|        slots, crcs = self._slots, self._crc","125|        mask = len(slots) - 1","126|        h = crc & mask","127|        while True:","128|            q = slots[h]","129|            if not q:","130|                return ~h","131|            if crcs[q - 1] == crc:","132|                return q - 1","133|            h = (h + 1) & mask","134|","135|    def _row(self, crc, ordinal):","136|        \"\"\"Row of the id (crc, ordinal), or -1.\"\"\"","137|        if ordinal == 0:","138|            p = self._slot(crc)","139|            return p if p >= 0 else -1","140|        more = self._more.get(crc)","141|        if more is None or not 0 < ordinal <= len(more):","142|            return -1","143|        return more[ordinal - 1]","144|","145|    def _add(self, crc, found, off, length):","146|        \"\"\"Append the row of a new blob in the open block; found = self._slot(crc).","147|        Return (row, ordinal).\"\"\"","148|        p = len(self._crc)","149|        self._crc.append(crc)","150|        self._blk.append(0)  # set at the flush","total_lines: 425","file_size: 17166","truncated: true","hint: Use offset=151 to continue reading (showing 1-150 of 425 lines)","is_binary: false","is_image: false","not_found: false"]};
  // DATA-END
  const N = DATA.ids.length;
  if (DATA.lines.length !== N || !DATA.ids.every(s => /^[0-9a-f]{8}$/.test(s)) || !/^[0-9a-f]{8}$/.test(DATA.crc)) throw new Error('levels/01.js: DATA is not a spine');

  // ── layout, in the units of this frame (the screen is 3.56 x 2 of them) ──────────────────────────────────────────
  const CX = -0.05, CY = 0, DISC = 1.6;                       // the disc of this level (def below): centre and R
  const ROWS = 53, COLS = Math.ceil(N / ROWS);                // three columns of 53 rows (the last has 51)
  const PITCH = 0.028, FS = 0.02, CW = 0.6 * FS;              // row pitch, text size, width of one character of the mono face
  const IDW = 8 * CW, TXT_DX = IDW + 0.03, CUT = 62;          // the id, the text 0.03 to its right, characters of a line that show
  const COLW = 0.93, COLWIDTH = TXT_DX + CUT * CW;            // column pitch; width of a column (the gap between two is 0.06)
  const LEFT = CX - (COLW * (COLS - 1) + COLWIDTH) / 2, Y0 = CY - PITCH * ROWS / 2;   // left edge of column 0, top edge of row 0
  const corner = Math.hypot((COLW * (COLS - 1) + COLWIDTH) / 2, PITCH * ROWS / 2);
  if (corner > DISC) throw new Error('levels/01.js: the page (corner at ' + corner.toFixed(3) + ') is not inside the disc R ' + DISC);
  const colOf = i => Math.floor(i / ROWS), colL = c => LEFT + c * COLW, rowY = i => Y0 + PITCH * ((i % ROWS) + 0.5);
  const colRows = c => Math.min(ROWS, N - c * ROWS);
  const TXT = DATA.lines.map(t => t.length > CUT ? t.slice(0, CUT - 1).trimEnd() + '…' : t);
  const TXW = TXT.map(t => t.length * CW);

  // level 0, the hero line, is placed in its row
  const BELOW = { n: 81, w: 1.944, x: 0, y: 0 };              // levels/00.js as F10 left it (F30): 81 cells of P = 0.024, the row's width, the row's centre = its frame's (0, 0)
  const K0 = CW / (BELOW.w / BELOW.n);                        // so that one byte of level 0 is one character of a row here
  const HERO = DATA.hero, HY = rowY(HERO), HX = colL(colOf(HERO)) + TXT_DX + CW * BELOW.n / 2;   // where the row of level 0 is centred
  const rowCx = i => colL(colOf(i)) + COLWIDTH / 2;

  // ── time, in seconds after the origin of the level (the middle of its flight in; the flight is 2.2 s) ─────────────
  const A0 = -0.45, A1 = 0.75;    // the rows fade in, the ones nearest the hero line first
  const S0 = 1.3, S1 = 3.1;       // the stroke runs down the ids
  const F0 = 3.25;                // the id of the whole text is written
  const dist = Array.from({ length: N }, (_, i) => Math.hypot(rowCx(i) - HX, rowY(i) - HY));
  const DMAX = Math.max(...dist);
  const AP = dist.map(d => A0 + (A1 - A0) * Math.pow(d / DMAX, 0.8));       // when row i starts to fade in
  const TS = Array.from({ length: N }, (_, i) => S0 + (S1 - S0) * i / N);   // when the stroke reaches row i

  // the id of the text, centred on the page; the strings near it step back (a hole in the page, not a plate)
  const BIG = 0.2, BW = 8 * 0.6 * BIG, BCX = CX, BCY = CY;
  const BX0 = BCX - BW / 2, BX1 = BCX + BW / 2, BY0 = BCY - 0.6 * BIG, BY1 = BCY + 0.6 * BIG;
  const clearAt = (x0, x1, y) => {
    const d = Math.hypot(Math.max(BX0 - x1, 0, x0 - BX1), Math.max(BY0 - y, 0, y - BY1));
    return 1 - 0.9 * (1 - seg(d, 0, 0.12));
  };
  const CLR_ID = Array.from({ length: N }, (_, i) => clearAt(colL(colOf(i)), colL(colOf(i)) + IDW, rowY(i)));
  const CLR_TX = Array.from({ length: N }, (_, i) => clearAt(colL(colOf(i)) + TXT_DX, colL(colOf(i)) + TXT_DX + TXW[i], rowY(i)));

  const ID_C = C.YELLOW;   // a line id, as levels/00.js draws its address (C.TEAL is this level's: the stroke and the id of the text)
  const hex = c => [1, 3, 5].map(k => parseInt(c.slice(k, k + 2), 16));
  const mix = (a, b, t) => { const A = hex(a), B = hex(b); return '#' + A.map((v, k) => Math.round(lerp(v, B[k], t)).toString(16).padStart(2, '0')).join(''); };

  // ── score: the page opens (plucks), the stroke runs (ticks, and a rising run of wood blocks: addresses that open), the id of
  // the text lands on one wood block and a bell. No drum: a drum is a fold, and this level has none. score.py scales the whole
  // file to a 0.9 peak, so the loudest event sets the gain of the film: the strike is vel 0.4 (at 0.7 this level became the
  // loudest second of the stub film and every other sound fell by about a fifth). ──
  const EV = [];
  for (let k = 0; k < 6; k++) EV.push([-0.3 + 0.16 * k, 'pluck', 1 + k, 0.1, -0.5 + 0.2 * k]);
  for (let i = 0; i < N; i += 4) EV.push([TS[i], 'tick', 0, 0.05, -0.7 + 0.7 * colOf(i)]);
  for (let k = 0; k < 10; k++) EV.push([S0 + (S1 - S0) * k / 10, 'wood', k, 0.26, -0.6 + 0.12 * k]);
  EV.push([F0 + 0.2, 'wood', 0, 0.4, 0], [F0 + 0.2, 'bell', 5, 0.2, 0]);   // the id of the text is the last address: a strike as it shows

  const me = F.def({
    name: 'spine', color: C.TEAL, R: DISC, cx: CX, cy: CY,
    zoom: 2.2, dwell: 4.6, lines: N, view: { x: CX, y: CY },
    hint: ['text = crc32c(spine)', 1.2],
    place: () => at(HX, HY, K0, 0, [BELOW.x, BELOW.y]),
    heroAlpha: u => 1 - seg(u, -0.55, 0.15),      // level 0 hands its row over to this level's own drawing of it
    draw(g, { u }) {
      const q = clamp((u - S0) / (S1 - S0)) * N;                              // rows that the stroke has passed
      const fin = seg(u, F0 + 0.05, F0 + 0.6), hole = seg(u, F0 - 0.2, F0 + 0.3);
      // the spine: a teal stroke along the ids of each column, drawn as the sweep goes down
      for (let c = 0; c < COLS; c++) {
        const k = clamp((q - c * ROWS) / colRows(c)), x = colL(c) - 0.022, h = PITCH * colRows(c);
        g.line(x, Y0, x, Y0 + h, { c: C.TEAL, w: 0.005, a: 0.85 * (1 - 0.45 * fin), k });
        if (k > 0 && k < 1) g.dot(x, Y0 + h * k, 0.011, C.WHITE, 0.9);
      }
      for (let i = 0; i < N; i++) {
        const app = i === HERO ? seg(u, -0.35, 0.25) : seg(u, AP[i], AP[i] + 0.45);
        if (app <= 0.004) continue;
        const x = colL(colOf(i)), y = rowY(i), since = u - TS[i];
        const settle = seg(since, 0, 0.3), flash = since < 0 ? 0 : Math.exp(-since * 9);
        const keep = i === HERO ? 1 : lerp(1, 0.5, fin);                      // the line that the camera came from keeps its id
        const aId = Math.min(1, lerp(0.5, 0.9, settle) * keep + 0.4 * flash) * app * lerp(1, CLR_ID[i], hole);
        const aTx = lerp(0.72, 0.4, settle) * lerp(1, 0.5, fin) * app * lerp(1, CLR_TX[i], hole);
        g.text(DATA.ids[i], x, y, { size: FS, font: 'mono', c: flash > 0.02 ? mix(ID_C, C.WHITE, flash) : ID_C, a: aId, align: 'left' });
        g.text(TXT[i], x + TXT_DX, y, { size: FS, font: 'mono', c: '#d3d8e0', a: aTx, align: 'left' });
      }
      g.text(DATA.crc, BCX, BCY, { size: BIG, font: 'mono', c: C.TEAL, a: 1, align: 'center', k: seg(u, F0, F0 + 0.8) });
    },
    // small: the page as three columns
    tiny(g) {
      for (let c = 0; c < COLS; c++) g.poly(rrectPts(colL(c) + COLWIDTH / 2, CY, COLWIDTH / 2, PITCH * ROWS / 2, 0.03),
        { c: C.TEAL, w: 0.03, a: 0.55, closed: true, fill: C.TEAL, fa: 0.2 });
    },
    ev: EV,
  });
})(Film);
