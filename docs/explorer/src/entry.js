// The explorer, live. desktop/plugin.js of the pin registers a page with register(ctx); it is bundled here as it is.
// sdk.js (the stand-in for the SDK) answers the page's ctx.rest() from JSON files in data/, so there is no server.
// This file mounts the page, and adds what a reader needs on a static page, around the page and not in it:
//   - a click on a session card shows that session; a click on any other card opens it at the levels 0, 0.5 and 1
//     (the text is the route GET /crumb, byte for byte: the page itself never shows a level);
//   - the wheel zooms (a CSS camera on the drawing area, about the pointer); a drag pans; a double click on the ground
//     goes back to fit.
import * as React from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient, rest, load } from '@hermes/plugin-sdk'
import plugin from 'pin-plugin'

const contributions = []
const ctx = {
  register: c => contributions.push(c),
  onEvent: () => {},
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  rest
}
plugin.register(ctx)
const page = contributions.find(c => c.id === 'page')
if (!page) throw new Error('the plugin registered no page')
createRoot(document.getElementById('root')).render(
  React.createElement(QueryClientProvider, { client: queryClient }, page.render())
)

// ---- around the page ------------------------------------------------------------------------------------------
const $ = (s, r = document) => r.querySelector(s)
const $$ = (s, r = document) => [...r.querySelectorAll(s)]
const sleep = ms => new Promise(r => setTimeout(r, ms))
async function until(fn, ms = 4000) {
  const t0 = performance.now()
  for (;;) {
    const v = fn()
    if (v) return v
    if (performance.now() - t0 > ms) return null
    await sleep(25)
  }
}
const stageEl = () => $('.sgp-body .sg-stage')
const bodyEl = () => $('.sgp-body')
const tabButton = name => $$('.sgp-tab').find(b => b.textContent === name)
const sessionSelect = () => { const l = $$('.sgp-bar label').find(l => l.firstChild && l.firstChild.textContent.trim() === 'Session'); return l ? $('select', l) : null }

let sessionOf = {} // the id of a session crumb -> the name of its session
load('graph.json').then(g => { sessionOf = Object.fromEntries(g.nodes.filter(n => n.kind === 'sess').map(n => [n.id, n.session])) })

function setSelect(sel, value) { // the way a person's choice reaches React: the native setter, then the change event
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, value)
  sel.dispatchEvent(new Event('change', { bubbles: true }))
}
async function showSession(sid) {
  const tab = tabButton('Session')
  if (tab && tab.dataset.on !== '1') tab.click()
  const sel = await until(() => { const s = sessionSelect(); return s && [...s.options].some(o => o.value === sid) ? s : null })
  if (sel && sel.value !== sid) setSelect(sel, sid)
}

// ---- the camera: a CSS transform on the drawing area ------------------------------------------------------------
const cam = { k: 1, x: 0, y: 0 }
const MAXK = 12
function clampCam() {
  const s = stageEl(), b = bodyEl()
  if (!s || !b) return
  const w = s.offsetWidth * cam.k, h = s.offsetHeight * cam.k
  cam.x = Math.min(0, Math.max(cam.x, Math.min(0, b.clientWidth - w)))
  cam.y = Math.min(0, Math.max(cam.y, Math.min(0, b.clientHeight - h)))
}
function applyCam() {
  const s = stageEl()
  if (!s) return
  s.style.transform = cam.k === 1 && cam.x === 0 && cam.y === 0 ? '' : `translate(${cam.x.toFixed(2)}px,${cam.y.toFixed(2)}px) scale(${cam.k.toFixed(5)})`
  s.dataset.zoom = cam.k.toFixed(3)
  placePanel()
}
const resetCam = () => { cam.k = 1; cam.x = 0; cam.y = 0; applyCam() }

document.addEventListener('wheel', e => {
  const b = e.target.closest && e.target.closest('.sgp-body')
  if (!b || !stageEl()) return // the Gantt and the Venn scroll as they do
  e.preventDefault()
  const r = b.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top
  const unit = e.deltaMode === 1 ? 0.05 : e.deltaMode === 2 ? 0.5 : 0.0016
  const k = Math.min(MAXK, Math.max(1, cam.k * Math.exp(-e.deltaY * unit * (e.ctrlKey ? 4 : 1))))
  cam.x = px - (px - cam.x) * k / cam.k
  cam.y = py - (py - cam.y) * k / cam.k
  cam.k = k
  clampCam()
  applyCam()
}, { passive: false })

let drag = null
document.addEventListener('pointerdown', e => {
  const b = e.button === 0 && e.target.closest && e.target.closest('.sgp-body')
  if (!b || !stageEl()) return
  drag = { x: e.clientX, y: e.clientY, cx: cam.x, cy: cam.y, moved: false, body: b, target: e.target }
}, true)
document.addEventListener('pointermove', e => {
  if (!drag) return
  const dx = e.clientX - drag.x, dy = e.clientY - drag.y
  if (!drag.moved && Math.hypot(dx, dy) < 5) return
  if (!drag.moved) { drag.moved = true; drag.body.classList.add('bc-drag') }
  cam.x = drag.cx + dx
  cam.y = drag.cy + dy
  clampCam()
  applyCam()
}, true)
const endDrag = e => {
  if (!drag) return
  const d = drag
  drag = null
  d.body.classList.remove('bc-drag')
  if (!d.moved && e.type === 'pointerup') onClick(d.target)
}
document.addEventListener('pointerup', endDrag, true)
document.addEventListener('pointercancel', endDrag, true)
document.addEventListener('dblclick', e => {
  if (e.target.closest && e.target.closest('.sgp-body') && !e.target.closest('.sg-node')) resetCam()
})

// ---- a click: a session card shows that session; any other card opens ----------------------------------------------
function onClick(t) {
  const node = t.closest && t.closest('.sg-node')
  const card = node && $('.sg-card', node)
  if (!node || !card || !card.dataset.lane) return closePanel() // the ground, or a lane label
  const id = node.dataset.id
  const sid = sessionOf[id]
  const here = sessionSelect()
  if (card.dataset.lane === 'session' && sid && !(here && here.value === sid)) return void showSession(sid)
  if (open && open.node === node) return closePanel() // a second click on the open card closes it
  openCard(node, lastLevel)
}

// ---- the open card: the route GET /crumb at the level 0, 0.5 or 1, in the card's own look ------------------------
const panel = document.createElement('div')
panel.id = 'bc-card'
panel.className = 'sg-card'
panel.hidden = true
panel.innerHTML = '<div class="bc-bar"><code class="bc-call"></code><span class="bc-levels"></span><button type="button" class="bc-x" aria-label="close">\u00d7</button></div><pre class="bc-text"></pre>'
document.body.appendChild(panel)
for (const level of ['0', '0.5', '1']) {
  const b = document.createElement('button')
  b.type = 'button'
  b.className = 'bc-lv'
  b.dataset.level = level
  b.textContent = level
  $('.bc-levels', panel).appendChild(b)
}
panel.addEventListener('click', e => {
  const lv = e.target.closest('.bc-lv')
  if (lv && open) { lastLevel = lv.dataset.level; openCard(open.node, lastLevel) }
  else if (e.target.closest('.bc-x')) closePanel()
})
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel() })

let open = null
let crumbs = null
let lastLevel = '1' // the level of the last choice: the next card opens at it
async function openCard(node, level) {
  const id = node.dataset.id
  crumbs = crumbs || await load('crumbs.json')
  const rec = crumbs[id] && crumbs[id][level]
  if (open && open.node !== node) open.node.classList.remove('sg-hi')
  open = { id, node, level }
  node.classList.add('sg-hi') // the page's own yellow halo
  const card = $('.sg-card', node)
  panel.style.setProperty('--lane', getComputedStyle(card).getPropertyValue('--lane').trim() || '#9399b2')
  panel.dataset.id = id
  panel.dataset.level = level
  $('.bc-call', panel).textContent = `crumb(${JSON.stringify(id)}, ${level})`
  for (const b of $$('.bc-lv', panel)) b.dataset.on = b.dataset.level === level ? '1' : '0'
  const text = rec ? rec.text : ''
  const head = text.split('\n', 1)[0]
  const h = document.createElement('span')
  h.className = 'bc-head'
  h.textContent = head
  $('.bc-text', panel).replaceChildren(h, document.createTextNode(text.slice(head.length)))
  panel.hidden = false
  placePanel()
}
function closePanel() {
  if (open) open.node.classList.remove('sg-hi')
  open = null
  panel.hidden = true
}
function placePanel() {
  if (!open || panel.hidden) return
  const r = open.node.getBoundingClientRect(), pw = panel.offsetWidth, ph = panel.offsetHeight
  let y = r.bottom + 10
  if (y + ph > innerHeight - 8) y = Math.max(8, r.top - ph - 10)
  panel.style.left = Math.max(8, Math.min(r.left, innerWidth - pw - 8)) + 'px'
  panel.style.top = y + 'px'
}

// the drawing area is made again (a tab, a session, a window size): the camera starts at fit and the card closes
let lastStage = null
new MutationObserver(() => {
  const s = stageEl()
  if (s === lastStage) return
  lastStage = s
  closePanel()
  resetCam()
}).observe(document.getElementById('root'), { childList: true, subtree: true })
