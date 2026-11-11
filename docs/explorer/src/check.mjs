// G03: the check of the live explorer.
//
//   node check.mjs [--shots DIR]
//
// A static server on site/ (the folder above this explorer), headless Chrome over CDP, and the page at /explorer/.
// Prints "ok ..." or "FAIL ..." for each item and exits 0 only if all are ok. Env: CHROME (the binary),
// G03_TMP (a scratch folder), G03_BC (a folder with bc-0.txt, bc-0.5.txt, bc-1.txt: the output of `bc d98e0bf6 L` of the pin).
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createServer } from 'node:http'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const explorer = resolve(here, '..')
const site = resolve(explorer, '..')
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const SCRATCH = process.env.G03_TMP || join(homedir(), '.hermes-a2a-jobs/cache/scratch')
const shotsDir = process.argv.includes('--shots') ? resolve(process.argv[process.argv.indexOf('--shots') + 1]) : null
const BAD = ['/Use' + 'rs/', 'file:' + '//', '/var/' + 'folders', '/priv' + 'ate/var']
const sleep = ms => new Promise(r => setTimeout(r, ms))

let failed = 0
const ok = (what, extra = '') => console.log(`ok   ${what}${extra ? ': ' + extra : ''}`)
const fail = (what, extra = '') => { failed++; console.log(`FAIL ${what}${extra ? ': ' + extra : ''}`) }
const expect = (cond, what, extra = '') => (cond ? ok(what, extra) : fail(what, extra))

// ---- a static server on site/, as GitHub Pages serves it ----
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.txt': 'text/plain; charset=utf-8', '.css': 'text/css' }
const served = []
const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://x')
  let file = resolve(site, '.' + decodeURIComponent(url.pathname))
  let status = 200
  if (!file.startsWith(site)) status = 403
  else if (existsSync(file) && statSync(file).isDirectory()) {
    if (!url.pathname.endsWith('/')) { served.push([301, url.pathname]); res.writeHead(301, { Location: url.pathname + '/' }); return res.end() }
    file = join(file, 'index.html')
  }
  if (status === 200 && !existsSync(file)) status = 404
  served.push([status, url.pathname])
  if (status !== 200) { res.writeHead(status); return res.end() }
  res.writeHead(200, { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' })
  res.end(readFileSync(file))
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const port = server.address().port

// ---- Chrome over CDP ----
mkdirSync(SCRATCH, { recursive: true })
const work = mkdtempSync(join(SCRATCH, 'g03-check-'))
const chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${join(work, 'chrome')}`, '--no-first-run',
  '--no-default-browser-check', '--mute-audio', '--force-color-profile=srgb', '--disable-gpu', '--use-mock-keychain', '--password-store=basic', 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] })
let ws
const finish = async code => {
  try { ws && ws.send(JSON.stringify({ id: 99999, method: 'Browser.close' })) } catch {}
  await sleep(300)
  try { chrome.kill('SIGKILL') } catch {}
  server.close()
  rmSync(work, { recursive: true, force: true })
  process.exit(code)
}
process.on('SIGINT', () => finish(1))
const cdpPort = await new Promise((res, rej) => {
  let buf = ''
  chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//); if (m) res(Number(m[1])) })
  chrome.on('exit', c => rej(new Error('chrome exited ' + c)))
  setTimeout(() => rej(new Error('chrome did not start')), 20000)
})
const target = (await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json()).find(t => t.type === 'page')
ws = new WebSocket(target.webSocketDebuggerUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('cdp socket')) })
let seq = 0
const waiting = new Map(), events = []
ws.onmessage = ev => {
  const m = JSON.parse(ev.data)
  if (m.id && waiting.has(m.id)) { const [res, rej] = waiting.get(m.id); waiting.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result) } else events.push(m)
}
const send = (method, params = {}) => new Promise((res, rej) => { waiting.set(++seq, [res, rej]); ws.send(JSON.stringify({ id: seq, method, params })) })
const ev = async expression => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (r.exceptionDetails) throw new Error('eval: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text))
  return r.result.value
}
const until = async (expression, what, ms = 10000) => {
  const t0 = Date.now()
  for (;;) {
    const v = await ev(expression).catch(() => false)
    if (v) return v
    if (Date.now() - t0 > ms) throw new Error(`timeout waiting for ${what}; page text: ${JSON.stringify(await ev('document.body.innerText.slice(0, 300)').catch(() => ''))}`)
    await sleep(60)
  }
}
const mouse = (type, x, y, extra = {}) => send('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: 1, ...extra })
const click = async (x, y) => { await mouse('mouseMoved', x, y); await mouse('mousePressed', x, y); await mouse('mouseReleased', x, y) }
const centre = async js => { const r = await ev(`(() => { const e = ${js}; if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height } })()`); if (!r) throw new Error('not found: ' + js); return r }
const clickOn = async js => { const c = await centre(js); await click(c.x, c.y); return c }
const shot = async name => { if (!shotsDir) return; mkdirSync(shotsDir, { recursive: true }); const r = await send('Page.captureScreenshot', { format: 'png' }); writeFileSync(join(shotsDir, name), Buffer.from(r.data, 'base64')) }

const NODES = `document.querySelectorAll('.sgp-body .sg-node').length + document.querySelectorAll('.sgp-body rect.sg-band').length`
const byHead = h => `[...document.querySelectorAll('.sg-node')].find(n => n.querySelector('.sg-head') && n.querySelector('.sg-head').textContent === ${JSON.stringify(h)})`
const tab = `(document.querySelector('.sgp-tab[data-on="1"]') || {}).textContent`
const sessSel = `(() => { const l = [...document.querySelectorAll('.sgp-bar label')].find(l => l.firstChild && l.firstChild.textContent.trim() === 'Session'); return l ? l.querySelector('select').value : '' })()`
const cards = lane => `[...document.querySelectorAll('.sgp-body .sg-card[data-lane="${lane}"] .sg-body')].map(e => e.textContent)`

try {
  for (const d of ['Page', 'Runtime', 'Log', 'Network']) await send(d + '.enable')
  await send('Emulation.setTimezoneOverride', { timezoneId: 'UTC' })
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
  await send('Page.navigate', { url: `http://127.0.0.1:${port}/explorer` }) // no slash: the server sends the redirect that GitHub Pages sends
  await until(`document.querySelectorAll('.sgp-tab').length >= 4 && ${NODES} > 0`, 'the page to draw')

  // 1. the first view
  const n0 = await ev(NODES)
  expect((await ev(tab)) === 'Session' && (await ev(sessSel)) === 'today' && n0 >= 20, 'the first view is the session "today" and draws 20 nodes or more', `${n0} nodes`)
  const tabs = await ev(`[...document.querySelectorAll('.sgp-tab')].map(b => b.textContent + (getComputedStyle(b).display === 'none' ? ' (hidden)' : ''))`)
  expect(tabs.join(',') === 'Map,Session,Gantt,Venn,Log graph (hidden),Log Gantt (hidden)', 'the six tabs of the page; the two that read state.db are hidden', tabs.join(', '))
  const back = await ev(`(() => { const a = document.querySelector('a[href="../"]'); const r = a.getBoundingClientRect(); return { x: r.x, y: r.y, to: new URL('../', location.href).pathname, text: a.textContent } })()`)
  expect(back.x < 40 && back.y < 40 && back.to === '/', 'a link at the top left goes back to ../', `"${back.text}" at ${Math.round(back.x)},${Math.round(back.y)} -> ${back.to}`)
  await shot('1-first.png')

  // 2. the Map: all sessions; a click on the session "tea"
  await clickOn(`[...document.querySelectorAll('.sgp-tab')].find(b => b.textContent === 'Map')`)
  await until(`${tab} === 'Map' && ${NODES} === 9`, 'the Map to draw 9 sessions')
  const mapHeads = await ev(`[...document.querySelectorAll('.sgp-body .sg-node .sg-head')].map(h => h.textContent).sort()`)
  expect(mapHeads.length === 9 && mapHeads.includes('session tea'), 'the Map draws the 9 sessions of the demo store', mapHeads.join(', '))
  await shot('2-map.png')
  await clickOn(byHead('session tea'))
  await until(`${tab} === 'Session' && ${sessSel} === 'tea' && ${NODES} !== 9 && document.querySelectorAll('.sg-card[data-lane="user"]').length === 3`, 'the session "tea" to draw')
  const users = await ev(cards('user')), assist = await ev(cards('assistant'))
  const nTea = await ev(NODES)
  expect(users[0].startsWith('The kettle boils too fast') && users[1].startsWith('Can I steep the same leaves twice?') && users[2].startsWith('Is oolong the same?') && assist.length === 3,
    'a click on the session "tea" shows the turns of that session', `${nTea} nodes; ${users.length} user turns, ${assist.length} assistant turns; first: "${users[0]}"`)
  const bands = await ev(`document.querySelectorAll('.sgp-body rect.sg-band').length`)
  expect(bands === 3, 'three turn bands', String(bands))
  await shot('3-tea.png')

  // 3. a click on a card opens it at the levels 0, 0.5 and 1: the text of the route, byte for byte
  const crumbs = JSON.parse(readFileSync(join(explorer, 'data/crumbs.json'), 'utf8'))
  await clickOn(`document.querySelector('.sg-node[data-id="d98e0bf6"]')`)
  await until(`!document.getElementById('bc-card').hidden`, 'the card to open')
  const read = () => ev(`({ id: document.getElementById('bc-card').dataset.id, level: document.getElementById('bc-card').dataset.level, call: document.getElementById('bc-card').querySelector('.bc-call').textContent, text: document.getElementById('bc-card').querySelector('.bc-text').textContent, hi: document.querySelector('.sg-node[data-id="d98e0bf6"]').classList.contains('sg-hi'), box: (r => ({ x: r.x, y: r.y, r: r.right, b: r.bottom }))(document.getElementById('bc-card').getBoundingClientRect()) })`)
  let o = await read()
  expect(o.id === 'd98e0bf6' && o.level === '1' && o.text === crumbs.d98e0bf6['1'].text && o.hi, 'the card opens at level 1 with the halo', JSON.stringify(o.text))
  expect(o.box.x >= 0 && o.box.y >= 0 && o.box.r <= 1440 && o.box.b <= 900, 'the open card is inside the window', JSON.stringify(o.box))
  const bc = process.env.G03_BC
  for (const lv of ['0', '0.5', '1']) {
    await clickOn(`document.querySelector('#bc-card .bc-lv[data-level="${lv}"]')`)
    await until(`document.getElementById('bc-card').dataset.level === '${lv}'`, 'level ' + lv)
    o = await read()
    const want = crumbs.d98e0bf6[lv].text
    const cli = bc && existsSync(join(bc, `bc-${lv}.txt`)) ? readFileSync(join(bc, `bc-${lv}.txt`), 'utf8') : null
    expect(o.text === want && o.call === `crumb("d98e0bf6", ${lv})` && (cli === null || cli === o.text), `level ${lv} shows the route's text` + (cli === null ? '' : ' and the output of `bc d98e0bf6 ' + lv + '`'), JSON.stringify(o.text))
  }
  await shot('4-card.png')
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await until(`document.getElementById('bc-card').hidden && !document.querySelector('.sg-node.sg-hi')`, 'Escape to close the card')
  ok('Escape closes the card and takes the halo away')
  const d98 = `document.querySelector('.sg-node[data-id="d98e0bf6"]')`
  await clickOn(d98)
  await until(`!document.getElementById('bc-card').hidden`, 'the card to open again')
  await clickOn(d98)
  await until(`document.getElementById('bc-card').hidden`, 'a second click to close the card')
  ok('a second click on the open card closes it')
  // the level of the last choice stays for the next card
  await clickOn(d98)
  await until(`!document.getElementById('bc-card').hidden`, 'the card to open')
  await clickOn(`document.querySelector('#bc-card .bc-lv[data-level="0"]')`)
  await until(`document.getElementById('bc-card').dataset.level === '0'`, 'level 0')
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await clickOn(`document.querySelector('.sg-node[data-id="2cb40b9b"]')`)
  await until(`!document.getElementById('bc-card').hidden && document.getElementById('bc-card').dataset.id === '2cb40b9b'`, 'the other card to open')
  o = await ev(`({ level: document.getElementById('bc-card').dataset.level, text: document.getElementById('bc-card').querySelector('.bc-text').textContent })`)
  expect(o.level === '0' && o.text === crumbs['2cb40b9b']['0'].text, 'the next card opens at the level of the last choice', JSON.stringify(o.text))
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await until(`document.getElementById('bc-card').hidden`, 'Escape')
  // the session card of the current session opens too: its text lists the turns
  await clickOn(byHead('session'))
  await until(`!document.getElementById('bc-card').hidden`, 'the session card to open')
  o = await ev(`document.getElementById('bc-card').querySelector('.bc-text').textContent`)
  expect(o === crumbs.cc45202b['0'].text, 'the card of the open session opens (level 0 here)', JSON.stringify(o))
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await until(`document.getElementById('bc-card').hidden`, 'Escape')

  // 4. the wheel zooms; a drag moves; a double click on the ground goes back to fit
  const stage = await centre(`document.querySelector('.sgp-body .sg-stage')`)
  const z = () => ev(`(() => { const s = document.querySelector('.sgp-body .sg-stage'); return { k: Number(s.dataset.zoom || 1), t: s.style.transform } })()`)
  await mouse('mouseMoved', stage.x, stage.y)
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: stage.x, y: stage.y, deltaX: 0, deltaY: -400 })
  await until(`Number(document.querySelector('.sg-stage').dataset.zoom) > 1.5`, 'the wheel to zoom in')
  let a = await z()
  expect(a.k > 1.5 && /scale\(/.test(a.t), 'the wheel zooms in', `x${a.k.toFixed(2)}`)
  const before = await centre(`document.querySelector('.sg-node[data-id="d98e0bf6"]')`)
  await mouse('mouseMoved', stage.x, stage.y)
  await mouse('mousePressed', stage.x, stage.y)
  await mouse('mouseMoved', stage.x + 60, stage.y + 40, { buttons: 1 })
  await mouse('mouseMoved', stage.x + 120, stage.y + 80, { buttons: 1 })
  await mouse('mouseReleased', stage.x + 120, stage.y + 80)
  const after = await centre(`document.querySelector('.sg-node[data-id="d98e0bf6"]')`)
  expect(Math.abs(after.x - before.x) > 20 || Math.abs(after.y - before.y) > 20, 'a drag moves the drawing', `card moved ${Math.round(after.x - before.x)},${Math.round(after.y - before.y)} px`)
  expect((await ev(`document.getElementById('bc-card').hidden`)), 'a drag opens no card')
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: stage.x, y: stage.y, deltaX: 0, deltaY: 6000 })
  await until(`Number(document.querySelector('.sg-stage').dataset.zoom) === 1`, 'the wheel to zoom out to fit')
  a = await z()
  expect(a.k === 1 && a.t === '', 'the wheel zooms out to fit', `x${a.k}`)
  // a double click on the ground goes back to fit
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: stage.x, y: stage.y, deltaX: 0, deltaY: -400 })
  await until(`Number(document.querySelector('.sg-stage').dataset.zoom) > 1.5`, 'the wheel to zoom in again')
  const g = await ev(`(() => { const b = document.querySelector('.sgp-body').getBoundingClientRect(); for (let y = b.y + 20; y < b.bottom - 20; y += 23) for (let x = b.x + 20; x < b.right - 20; x += 29) { const e = document.elementFromPoint(x, y); if (e && e.closest('.sgp-body') && !e.closest('.sg-node')) return { x, y } } return null })()`)
  await mouse('mouseMoved', g.x, g.y)
  for (const n of [1, 2]) { await mouse('mousePressed', g.x, g.y, { clickCount: n }); await mouse('mouseReleased', g.x, g.y, { clickCount: n }) }
  await until(`Number(document.querySelector('.sg-stage').dataset.zoom) === 1`, 'a double click to go back to fit')
  ok('a double click on the ground goes back to fit')
  await shot('5-zoom.png')

  // the page's own Zoom box (fit, 1.5x, 2x, 3x, 4x) still works with the camera: it draws larger, and a drag moves it
  const setSelect = (label, value) => ev(`(() => { const l = [...document.querySelectorAll('.sgp-bar label')].find(l => l.firstChild && l.firstChild.textContent.trim() === ${JSON.stringify(label)}); const s = l.querySelector('select'); Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(s, ${JSON.stringify(value)}); s.dispatchEvent(new Event('change', { bubbles: true })) })()`)
  const stageWidth = () => ev(`document.querySelector('.sgp-body .sg-stage').offsetWidth`)
  const dragBy = async (x, y, dx, dy) => {
    await mouse('mouseMoved', x, y); await mouse('mousePressed', x, y)
    await mouse('mouseMoved', x + dx / 2, y + dy / 2, { buttons: 1 }); await mouse('mouseMoved', x + dx, y + dy, { buttons: 1 })
    await mouse('mouseReleased', x + dx, y + dy)
  }
  const wFit = await stageWidth()
  await setSelect('Zoom', '2')
  await until(`document.querySelector('.sgp-body .sg-stage').offsetWidth > ${wFit} * 1.8`, "the page's Zoom box to draw 2x")
  const big = await stageWidth()
  const cardBox = `document.querySelector('.sg-node[data-id="d98e0bf6"]')`
  const c0 = await centre(cardBox)
  await dragBy(stage.x, stage.y, -80, -60)
  const c1 = await centre(cardBox)
  expect(Math.abs(c1.x - c0.x + 80) < 2 && Math.abs(c1.y - c0.y + 60) < 2, "the page's Zoom box (2x) draws larger and a drag moves it", `stage ${wFit} -> ${big} px wide; card moved ${Math.round(c1.x - c0.x)},${Math.round(c1.y - c0.y)} px`)
  await setSelect('Zoom', '1')
  await until(`document.querySelector('.sgp-body .sg-stage').offsetWidth <= ${wFit} + 2 && Number(document.querySelector('.sg-stage').dataset.zoom) === 1`, 'the Zoom box back to fit')
  ok("the page's Zoom box back to fit")

  // the page polls its routes every 5 s (a refetch of the same files): an open card and the camera must stay
  await clickOn(cardBox)
  await until(`!document.getElementById('bc-card').hidden`, 'the card to open')
  await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: stage.x, y: stage.y, deltaX: 0, deltaY: -300 })
  await until(`Number(document.querySelector('.sg-stage').dataset.zoom) > 1.3`, 'the wheel to zoom in')
  const mark = await ev(`(() => { const s = document.querySelector('.sgp-body .sg-stage'); s.__g03 = 1; const p = document.getElementById('bc-card'); return { t: s.style.transform, id: p.dataset.id, level: p.dataset.level, updated: document.querySelector('.sgp-body').dataset.sgUpdated } })()`)
  await sleep(6500)
  const kept = await ev(`(() => { const s = document.querySelector('.sgp-body .sg-stage'), p = document.getElementById('bc-card'); return { same: s.__g03 === 1, t: s.style.transform, open: !p.hidden, id: p.dataset.id, level: p.dataset.level, hi: document.querySelector('.sg-node.sg-hi') && document.querySelector('.sg-node.sg-hi').dataset.id, updated: document.querySelector('.sgp-body').dataset.sgUpdated } })()`)
  expect(kept.same && kept.t === mark.t && kept.open && kept.id === mark.id && kept.level === mark.level && kept.hi === 'd98e0bf6', 'across a poll of the page (6.5 s) the open card, the halo and the zoom stay',
    `the page fetched again: ${mark.updated !== kept.updated}`)
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
  await until(`document.getElementById('bc-card').hidden`, 'Escape')

  // the window: no sideways scroll at 1280 and at 390 px, and the open card stays inside the window
  for (const [w, h] of [[1280, 720], [390, 800]]) {
    await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false })
    await until(`window.innerWidth === ${w} && document.querySelectorAll('.sgp-body .sg-node').length > 0 && document.querySelector('.sg-stage').offsetWidth <= ${w}`, `the page at ${w} px`)
    await sleep(300)
    const fit = await ev(`(() => { const b = document.querySelector('.sgp-body').getBoundingClientRect(), s = document.querySelector('.sg-stage'); return { scrollW: document.documentElement.scrollWidth, scrollH: document.documentElement.scrollHeight, innerH: innerHeight, bodyW: Math.round(b.width), bodyH: Math.round(b.height), stageW: s.offsetWidth, stageH: s.offsetHeight } })()`)
    expect(fit.scrollW <= w && fit.scrollH <= fit.innerH && fit.stageW <= fit.bodyW + 1 && fit.stageH <= fit.bodyH + 1 && fit.bodyH > 150, `no sideways scroll at ${w} px; the drawing fits its box`, JSON.stringify(fit))
    await clickOn(cardBox)
    await until(`!document.getElementById('bc-card').hidden`, `the card to open at ${w} px`)
    const pb = await ev(`(r => ({ x: r.x, y: r.y, r: r.right, b: r.bottom }))(document.getElementById('bc-card').getBoundingClientRect())`)
    expect(pb.x >= 0 && pb.y >= 0 && pb.r <= w && pb.b <= h, `the open card is inside the window at ${w} px`, JSON.stringify(pb))
    await shot(`6-${w}.png`)
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 })
    await until(`document.getElementById('bc-card').hidden`, 'Escape')
  }
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false })
  await until(`window.innerWidth === 1440 && document.querySelectorAll('.sgp-body .sg-node').length > 0`, 'the page at 1440 px')

  // 5. the other tabs of the page run on the files too
  await clickOn(`[...document.querySelectorAll('.sgp-tab')].find(b => b.textContent === 'Session')`)
  const sel = await ev(`(() => { const l = [...document.querySelectorAll('.sgp-bar label')].find(l => l.firstChild && l.firstChild.textContent.trim() === 'Session'); return [...l.querySelectorAll('option')].map(o => o.value) })()`)
  expect(sel.length === 9 && sel.includes('tea') && sel.includes('today'), 'the Session box lists the 9 sessions', sel.join(', '))
  // from the session "today": its card of the session "tea" (a recall target) shows that session too
  await ev(`(() => { const l = [...document.querySelectorAll('.sgp-bar label')].find(l => l.firstChild && l.firstChild.textContent.trim() === 'Session'); const s = l.querySelector('select'); Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(s, 'today'); s.dispatchEvent(new Event('change', { bubbles: true })) })()`)
  await until(`${sessSel} === 'today' && ${NODES} >= 20`, 'today to draw again')
  const todaySess = await ev(`[...document.querySelectorAll('.sg-card[data-lane="session"] .sg-head')].map(h => h.textContent)`)
  if (todaySess.includes('session tea')) {
    await clickOn(byHead('session tea'))
    await until(`${sessSel} === 'tea' && document.querySelectorAll('.sg-card[data-lane="user"]').length === 3`, 'a click on "session tea" in the view of today')
    ok('in the view of "today" a click on its card "session tea" shows that session', todaySess.join(', '))
  } else ok('the view of "today" has no card of another session', todaySess.join(', '))
  await clickOn(`[...document.querySelectorAll('.sgp-tab')].find(b => b.textContent === 'Gantt')`)
  await until(`document.querySelector('.sgp-svg svg')`, 'the Gantt')
  ok('the tab Gantt draws', await ev(`document.querySelector('.sgp-svg svg').querySelectorAll('rect').length + ' rects'`))
  await clickOn(`[...document.querySelectorAll('.sgp-tab')].find(b => b.textContent === 'Venn')`)
  const venn = await until(`document.querySelector('.sgp-bar input[type=range]') && document.querySelector('.sgp-svg svg') && [...document.querySelectorAll('.sgp-bar label')].map(l => l.textContent).find(t => /^Event \\d+ of \\d+/.test(t))`, 'the Venn')
  ok('the tab Venn draws', venn)
  // the slider moves the Venn to another event; the numbers on it are the numbers of the file
  const vennFile = JSON.parse(readFileSync(join(explorer, 'data/venn/today-4.json'), 'utf8'))
  const nf = new Intl.NumberFormat('en-US')
  const wantText = Object.values(vennFile.sets).filter(s => s.ids.length).map(s => `${nf.format(s.bytes)} B \u00b7 ${s.ids.length}`)
  const vennBefore = await ev(`document.querySelector('.sgp-svg svg').textContent`)
  await ev(`(() => { const r = document.querySelector('.sgp-bar input[type=range]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(r, '4'); r.dispatchEvent(new Event('input', { bubbles: true })) })()`)
  await until(`[...document.querySelectorAll('.sgp-bar label')].some(l => /^Event 5 of 12/.test(l.textContent)) && document.querySelector('.sgp-svg svg') && document.querySelector('.sgp-svg svg').textContent !== ${JSON.stringify(vennBefore)}`, 'the slider to move the Venn to event 5')
  const vennAfter = await ev(`document.querySelector('.sgp-svg svg').textContent`)
  expect(wantText.length === 3 && wantText.every(t => vennAfter.includes(t)), 'the slider moves the Venn to event 5 of 12; its three sets carry the numbers of data/venn/today-4.json', wantText.join(' | '))
  await shot('7-venn.png')
  await sleep(1500)

  // 6. the console and the network
  const errors = [], warnings = []
  for (const e of events) {
    if (e.method === 'Runtime.consoleAPICalled' && ['error', 'assert'].includes(e.params.type)) errors.push('console.' + e.params.type + ': ' + e.params.args.map(x => x.value ?? x.description ?? x.type).join(' '))
    else if (e.method === 'Runtime.consoleAPICalled' && e.params.type === 'warning') warnings.push(e.params.args.map(x => x.value ?? x.description ?? x.type).join(' '))
    else if (e.method === 'Runtime.exceptionThrown') errors.push('exception: ' + (e.params.exceptionDetails.exception?.description || e.params.exceptionDetails.text))
    else if (e.method === 'Log.entryAdded' && e.params.entry.level === 'error') errors.push('log: ' + e.params.entry.text + ' ' + (e.params.entry.url || ''))
    else if (e.method === 'Log.entryAdded' && e.params.entry.level === 'warning') warnings.push('log: ' + e.params.entry.text)
  }
  expect(errors.length === 0, 'no console error', `${errors.length} errors, ${warnings.length} warnings` + (errors.length ? ' ' + JSON.stringify(errors).slice(0, 600) : ''))
  const requests = new Map(), failedReq = []
  for (const e of events) {
    if (e.method === 'Network.requestWillBeSent') requests.set(e.params.requestId, { url: e.params.request.url, status: null })
    else if (e.method === 'Network.responseReceived') { const r = requests.get(e.params.requestId); if (r) r.status = e.params.response.status }
    else if (e.method === 'Network.loadingFailed') failedReq.push(requests.get(e.params.requestId)?.url + ' ' + e.params.errorText)
  }
  const list = [...requests.values()]
  const outside = list.filter(r => !r.url.startsWith(`http://127.0.0.1:${port}/`) && !r.url.startsWith('data:'))
  const notOk = list.filter(r => !r.url.startsWith('data:') && r.status !== 200 && r.status !== 301)
  expect(failedReq.length === 0 && notOk.length === 0 && outside.length === 0, 'no failed request, and none leaves the server', `${list.length} requests, ${list.filter(r => r.status === 200).length} answered 200, ${served.filter(s => s[0] >= 400).length} answered 4xx/5xx by the server` + (notOk.length ? ' BAD: ' + JSON.stringify(notOk) : ''))
  expect(served.filter(s => s[0] >= 400).length === 0, 'the server answered no 4xx', served.filter(s => s[0] >= 400).map(s => s.join(' ')).join(', '))
} catch (e) {
  fail('the run', e.message)
}

// 7. the files of the folder agree with each other: the bundle is the one that BUILD.txt names, and LICENSES.txt holds a
// license for the page and for each package that is inside the bundle
{
  const build = readFileSync(join(explorer, 'BUILD.txt'), 'utf8'), lic = readFileSync(join(explorer, 'LICENSES.txt'), 'utf8')
  const bundle = readFileSync(join(explorer, 'bundle.js'))
  const named = /^bundle\.js (\d+) bytes, sha256 ([0-9a-f]{64})/m.exec(build)
  expect(named && Number(named[1]) === bundle.length && named[2] === createHash('sha256').update(bundle).digest('hex'), 'bundle.js is the file that BUILD.txt names (size and sha256)', named ? `${named[1]} bytes, ${named[2].slice(0, 12)}` : 'no such line in BUILD.txt')
  const inside = ((/^inside\s+(.+)$/m.exec(build) || [])[1] || '').split(', ').filter(Boolean)
  const missing = inside.filter(p => !new RegExp('^== ' + p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ' \\(MIT\\) ==\\n\\S', 'm').test(lic))
  expect(inside.length >= 6 && missing.length === 0 && /^== hermes-breadcrumb-plugin: desktop\/plugin\.js at [0-9a-f]+ \(the page; MIT\) ==\nMIT License/m.test(lic),
    'LICENSES.txt holds a license for the page and for each package inside the bundle (MIT)', `${inside.length} packages: ${inside.join(', ')}` + (missing.length ? ' MISSING: ' + missing.join(', ') : ''))
}

// 8. the strings of this machine: none in a file of the folder
const hits = []
const walk = d => { for (const n of readdirSync(d)) { const p = join(d, n); if (statSync(p).isDirectory()) walk(p); else { const t = readFileSync(p, 'utf8'); for (const b of BAD) if (t.includes(b)) hits.push(`${p.slice(explorer.length + 1)} has "${b}"`) } } }
walk(explorer)
expect(hits.length === 0, 'no string of this machine (an absolute path of a home, a file URL, a temp path) in any file of the folder', hits.join('; '))
const total = (function sum(d) { return readdirSync(d).reduce((s, n) => { const p = join(d, n); return s + (statSync(p).isDirectory() ? sum(p) : statSync(p).size) }, 0) })(explorer)
console.log(failed ? `${failed} FAILED` : `all checks passed (the folder holds ${(total / 1024).toFixed(0)} KB)`)
await finish(failed ? 1 : 0)
