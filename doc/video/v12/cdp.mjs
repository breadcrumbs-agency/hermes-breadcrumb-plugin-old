// A small driver for the Chrome that is installed: the DevTools protocol over the WebSocket that Node has built in.
// No package and no browser download. Each run gets a new profile in the temp folder, and close() deletes it.
// The page object has the few calls that render.mjs needs, named as Playwright names them:
//   const browser = await launch();  const pg = await browser.newPage({ viewport: { width, height } });
//   pg.on('pageerror', message => …)   fires for an uncaught error, a console.error and a resource that fails to load
//   await pg.goto(url);  await pg.evaluate(fn, arg)  (the promise that fn returns is awaited)  await browser.close();
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = ms => new Promise(r => setTimeout(r, ms));
const text = d => d?.exception?.description || d?.text || 'unknown error';

export async function launch() {
  if (!existsSync(CHROME)) throw new Error(`Chrome is not at ${CHROME} (set CHROME to its path)`);
  const profile = mkdtempSync(join(tmpdir(), 'v12-chrome-'));
  const proc = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run',
    '--no-default-browser-check', '--hide-scrollbars', '--disable-gpu', 'about:blank'], { stdio: 'ignore' });
  const exited = new Promise(r => proc.once('exit', r));
  const wipe = () => { try { rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); } catch { /* the temp folder is pruned */ } };
  let finished = false;
  const hardStop = () => { if (finished) return; finished = true; try { proc.kill('SIGKILL'); } catch { /* gone */ } wipe(); };
  process.on('exit', hardStop);                                   // an error or a signal must not leave a Chrome or a profile
  for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => process.exit(130));

  let port = null, path = null;
  for (let i = 0; i < 150 && !port; i++) {
    const f = join(profile, 'DevToolsActivePort');
    if (existsSync(f)) { const [p, w] = readFileSync(f, 'utf8').split('\n'); if (p && w) { port = p; path = w; } }
    if (!port) { if (proc.exitCode !== null) break; await sleep(100); }
  }
  if (!port) { hardStop(); throw new Error('Chrome did not start'); }

  const ws = new WebSocket(`ws://127.0.0.1:${port}${path}`);
  await new Promise((ok, no) => { ws.onopen = ok; ws.onerror = () => no(new Error('cannot connect to Chrome')); }).catch(e => { hardStop(); throw e; });
  let id = 0;
  const calls = new Map(), sessions = new Map();
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id) {
      const c = calls.get(m.id); calls.delete(m.id);
      if (c) { clearTimeout(c.timer); m.error ? c.no(new Error(`${c.method}: ${m.error.message}`)) : c.ok(m.result); }
    } else sessions.get(m.sessionId)?.(m.method, m.params);
  };
  ws.onclose = () => { for (const c of calls.values()) { clearTimeout(c.timer); c.no(new Error(`${c.method}: Chrome closed`)); } calls.clear(); };
  const send = (method, params = {}, sessionId, ms = 120000) => new Promise((ok, no) => {
    const i = ++id, timer = setTimeout(() => { calls.delete(i); no(new Error(`${method}: no answer in ${ms / 1000} s`)); }, ms);
    calls.set(i, { ok, no, method, timer });
    ws.send(JSON.stringify({ id: i, method, params, sessionId }));
  });

  async function newPage({ viewport }) {
    // A window of its own (F31): a page that is made while another is in front is hidden, and a hidden page never settles the
    // img.decode() of levels/07.js, so the extra pages of render.mjs (made at one time) waited for ever.
    const { targetId } = await send('Target.createTarget', { url: 'about:blank', newWindow: true });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    const listeners = { pageerror: [] }, once = new Map();
    const fail = m => listeners.pageerror.forEach(f => f(m));
    sessions.set(sessionId, (method, p) => {
      if (once.has(method)) { once.get(method)(p); once.delete(method); }
      if (method === 'Runtime.exceptionThrown') fail(`page error: ${text(p.exceptionDetails).split('\n').slice(0, 3).join(' | ')}`);
      else if (method === 'Runtime.consoleAPICalled' && (p.type === 'error' || p.type === 'assert')) fail(`console error: ${p.args.map(a => a.value ?? a.description).join(' ')}`);
      else if (method === 'Log.entryAdded' && p.entry.level === 'error') fail(`load error: ${p.entry.text}${p.entry.url ? ' ' + p.entry.url : ''}`);
    });
    const cmd = (m, p) => send(m, p, sessionId);
    await Promise.all([cmd('Page.enable'), cmd('Runtime.enable'), cmd('Log.enable')]);
    await cmd('Emulation.setDeviceMetricsOverride', { width: viewport.width, height: viewport.height, deviceScaleFactor: 1, mobile: false });
    return {
      on: (event, f) => listeners[event].push(f),
      async goto(url) {
        const loaded = new Promise(r => once.set('Page.loadEventFired', r));
        const r = await cmd('Page.navigate', { url });
        if (r.errorText) throw new Error(`cannot open ${url}: ${r.errorText}`);
        await loaded;
      },
      async evaluate(fn, arg) {
        const expression = typeof fn === 'function' ? `(${fn})(${JSON.stringify(arg)})` : String(fn);
        const r = await cmd('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(text(r.exceptionDetails));
        return r.result.value;
      },
    };
  }

  async function close() {
    if (finished) return;
    try { await Promise.race([send('Browser.close', {}, undefined, 3000), sleep(3000)]); } catch { /* Chrome drops the socket as it ends */ }
    await Promise.race([exited, sleep(5000)]);
    try { ws.close(); } catch { /* closed */ }
    hardStop();
  }
  return { newPage, close };
}
