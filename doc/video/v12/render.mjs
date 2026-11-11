// Builds the film: the score from the film's own clock, then every frame rendered in headless Chrome and piped
// in order into ffmpeg with the score as its soundtrack.
//   node render.mjs out.mp4 [fps=60] [workers=4] [width=1920]   the film, and score.m4a for the player page
//   node render.mjs --stills dir t1 t2 …                        PNG stills for review (dir/a-<t>.png)
//   node render.mjs --meta out.json                             score data alone (events, ascent curve)
// Add  --levels 00,03,stubs/01  anywhere to name the level files: a bare name is levels/<name>.js, and a name with a
// slash is a path from this folder. Without it the film takes every levels/*.js, in name order.
// Chrome is the one that is installed (CHROME=… to change it). The score needs NumPy (PYTHON=… ; default python3.14).
// The exit code is 1 if the page threw, logged an error or failed to load a file.
import { execFileSync, spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { launch } from './cdp.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const PY = process.env.PYTHON ?? 'python3.14';
let levelArg = null;
const li = args.indexOf('--levels');
if (li >= 0) { levelArg = args[li + 1] ?? ''; args.splice(li, 2); }
const width = args[0] === '--stills' || args[0] === '--meta' ? 1920 : Number(args[3] ?? 1920);

const levelsDir = join(here, 'levels');
const levels = (levelArg !== null ? levelArg.split(',').filter(Boolean)
  : existsSync(levelsDir) ? readdirSync(levelsDir).filter(f => f.endsWith('.js')).sort().map(f => f.slice(0, -3)) : [])
  .map(n => n.includes('/') ? n : `levels/${n}`);
const missing = levels.filter(n => !existsSync(join(here, `${n}.js`)));
if (!levels.length || missing.length) {
  console.error(missing.length ? `no such level file: ${missing.map(n => n + '.js').join(', ')}` : 'no level files in levels/ (name some with --levels, e.g. --levels stubs/00,stubs/01)');
  process.exit(1);
}
const url = pathToFileURL(join(here, 'index.html')).href + `?capture&w=${width}&levels=${encodeURIComponent(levels.join(','))}`;

let errors = 0;
async function page(browser) {
  const pg = await browser.newPage({ viewport: { width, height: Math.round(width * 9 / 16) } });
  pg.on('pageerror', m => { errors++; console.error(m); });
  await pg.goto(url);
  await pg.evaluate(() => window.ready);
  await pg.evaluate(() => Promise.all([
    document.fonts.load('20px KaTeXMath', 'αλ'), document.fonts.load('20px KaTeXMain', 'a⋈∘→·≠'), document.fonts.load('20px KaTeXAMS', '↻⇄'),
    document.fonts.load("20px 'JetBrains Mono'", 'a'),
  ]).then(() => document.fonts.ready));
  return pg;
}
const grab = (pg, t, type = 'image/jpeg') => pg.evaluate(([t, type]) => { renderAt(t); return document.getElementById('cv').toDataURL(type, 0.94).split(',')[1]; }, [t, type]).then(b => Buffer.from(b, 'base64'));
const meta = pg => pg.evaluate(() => ({ duration: Film.duration, events: Film.events, curve: Film.curve(100), levels: Film.levels }));

const browser = await launch();
try {
  if (args[0] === '--stills') {
    const dir = resolve(args[1]); mkdirSync(dir, { recursive: true });
    const pg = await page(browser);
    for (const t of args.slice(2)) writeFileSync(join(dir, `a-${t}.png`), await grab(pg, Number(t), 'image/png'));
  } else if (args[0] === '--meta') {
    writeFileSync(resolve(args[1]), JSON.stringify(await meta(await page(browser))));
  } else {
    const out = resolve(args[0] ?? 'film.mp4'), fps = Number(args[1] ?? 60), workers = Number(args[2] ?? 4);
    const first = await page(browser), tmp = mkdtempSync(join(tmpdir(), 'v12-'));
    try {
      const film = await meta(first), wav = join(tmp, 'score.wav'), total = Math.round(film.duration * fps);
      writeFileSync(join(tmp, 'meta.json'), JSON.stringify(film));
      execFileSync(PY, [join(here, 'score.py'), join(tmp, 'meta.json'), wav], { stdio: 'inherit' });
      execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-i', wav, '-c:a', 'aac', '-b:a', '160k', join(here, 'score.m4a')]);
      const pages = [first, ...await Promise.all(Array.from({ length: workers - 1 }, () => page(browser)))];
      const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-', '-i', wav,
        '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '17', '-preset', 'medium', '-c:a', 'aac', '-b:a', '192k',
        '-shortest', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
      const pending = new Map(); let next = 0, written = 0, writing = false;
      const flush = async () => {
        if (writing) return; writing = true;
        while (pending.has(written)) { const b = pending.get(written); pending.delete(written); written++; if (!ff.stdin.write(b)) await once(ff.stdin, 'drain'); }
        writing = false;
      };
      const t0 = Date.now();
      await Promise.all(pages.map(async pg => {
        for (let f; (f = next++) < total;) {
          while (f - written > 240) await new Promise(r => setTimeout(r, 20));
          pending.set(f, await grab(pg, f / fps));
          await flush();
          if (f % 600 === 0) console.log(`${f}/${total} ${((Date.now() - t0) / 1000).toFixed(0)}s`);
        }
      }));
      await flush(); ff.stdin.end(); await once(ff, 'close');
      console.log(`wrote ${out} (${total} frames, ${film.duration.toFixed(2)}s)`);
    } finally { rmSync(tmp, { recursive: true, force: true }); }
  }
} finally { await browser.close(); }
if (errors) { console.error(`${errors} page error${errors > 1 ? 's' : ''}`); process.exitCode = 1; }
