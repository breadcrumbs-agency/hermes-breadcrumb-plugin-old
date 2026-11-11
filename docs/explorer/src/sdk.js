// The stand-in for "@hermes/plugin-sdk". desktop/plugin.js of the pin takes from it: host, three areas, queryClient,
// useQuery and useValue. In Hermes Desktop the app provides them. Here useQuery and useValue are the same two
// libraries that the SDK re-exports (@tanstack/react-query, @nanostores/react); the host state is two plain atoms
// (the page reads the focused session and the focused profile from them); nothing else of the SDK is used.
// (The names are the same as stage/sdk.js of the README lane, P01.)
//
// What P01 does with a server, this does with files. In the Desktop the page's ctx.rest(path) asks the plugin
// routes of a running Hermes. Here `rest` answers the same paths from the JSON files in data/ (what
// dashboard/plugin_api.py of the pin gave for the seeded demo store; src/make-data.py wrote them). There is no server.
import { QueryClient, useQuery } from '@tanstack/react-query'
import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar-nav'
export const PALETTE_AREA = 'palette'

export const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })

export const host = {
  state: { focusedStoredSessionId: atom(null), focusedSessionProfile: atom(null) },
  navigate: () => {}
}

export { useQuery }
export const useValue = useStore

// ---- the routes: files in place of HTTP -------------------------------------------------------------------------
const script = document.currentScript // the folder of this bundle, wherever the site is served from
const BASE = new URL('data/', script && script.src ? script.src : location.href)
const files = new Map()

export function load(rel) { // one file, read once
  if (!files.has(rel)) {
    files.set(rel, fetch(new URL(rel, BASE)).then(res => {
      if (!res.ok) throw new Error(`${res.status} ${rel}`)
      return res.json()
    }).catch(e => { files.delete(rel); throw e }))
  }
  return files.get(rel)
}

const part = s => encodeURIComponent(s || '')

export function rest(path) { // the paths that the page asks for (dashboard/plugin_api.py of the pin)
  const url = new URL(path, 'http://route/'), q = url.searchParams
  switch (url.pathname) {
    case '/stores': return load('stores.json')
    case '/graph': return load('graph.json')
    case '/session': return load(`session/${part(q.get('sid'))}.json`)
    case '/context': return load(`context/${part(q.get('sid'))}.json`)
    case '/venn': return load(`venn/${part(q.get('sid'))}-${part(q.get('at'))}.json`)
    case '/log/sessions': return Promise.resolve({ sessions: [] }) // the log tabs read state.db of a Hermes: none here (the tabs are hidden)
    default: return Promise.resolve({ error: `not found: ${url.pathname}` })
  }
}
