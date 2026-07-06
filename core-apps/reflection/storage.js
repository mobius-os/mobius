import { useEffect, useState } from 'react'
import { CHAT_OPEN_VERSION, CHAT_RATIO_VERSION } from './constants.js'

// ---------------------------------------------------------------------------
// Storage — raw fetch with the app token (per the data contract). JSON paths
// parse JSON; report bodies are read as text. A real 404 (storage empty on
// first run) is normal and returns the `notFound` shape so callers can tell
// it apart from a network failure (`error`) and treat each correctly.
// ---------------------------------------------------------------------------

export function makeStorage(appId, token) {
  const ms = (typeof window !== 'undefined' && window.mobius && window.mobius.storage) || null
  const headers = { Authorization: `Bearer ${token}` }
  const base = `/api/storage/apps/${appId}`
  const listBase = `/api/storage/apps-list/${appId}`

  async function getJSON(path) {
    try {
      if (ms && typeof ms.get === 'function') {
        const data = await ms.get(path)
        return data == null ? { notFound: true } : { data }
      }
      const r = await fetch(`${base}/${path}`, { headers })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.json() }
    } catch {
      return { error: 0 }
    }
  }

  // The honest save. durableWrite RESOLVES { durability:'synced'|'queued' } —
  // BOTH are durable: 'synced' is server-accepted, 'queued' is outboxed offline
  // with a guaranteed retry (NOT a failure). It REJECTS a DurableWriteError only
  // when the server FATALLY refuses the write (413 quota / 400 / 403): that
  // rejection is the truth the old re-read dance had to reconstruct, so we let it
  // propagate to the caller, which turns it into an error instead of "Saved".
  async function putJSON(path, obj) {
    const dw = (typeof window !== 'undefined' && window.mobius && window.mobius.durableWrite) || null
    if (dw) {
      // Resolve (synced OR queued) = durable success; a fatal reject throws
      // DurableWriteError, which the call site catches and surfaces as an error.
      return await dw(path, obj)
    }
    // Standalone fallback (no window.mobius bridge): a raw PUT, throwing on any
    // non-2xx so the caller treats it exactly like a fatal durableWrite reject.
    // Return the same { durability } shape so callers never special-case the path.
    const r = await fetch(`${base}/${path}`, {
      method: 'PUT',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    })
    if (!r.ok) throw new Error(`PUT ${path} failed (${r.status})`)
    return { durability: 'synced', path }
  }

  async function getReportHtml(name) {
    // Report bodies are raw HTML documents — read as TEXT through the runtime's
    // typed, read-through store (getText). That mirror is what lets a brief
    // OPEN OFFLINE: an online read caches the body, and a later offline read
    // serves the last-known copy (overlaid with any pending write). It also
    // makes the body appear in the offline reports listing — list() derives its
    // offline entries from exactly the paths this app has read into the cache.
    // The cron can RE-AUTHOR a brief for the same date; the runtime revisions
    // the cache on every online read, so reopening online always reconciles to
    // the freshly-authored body (no stale-cache pin). Standalone (no bridge)
    // falls back to a raw text fetch with `no-store` so a re-authored brief
    // never serves stale there either.
    try {
      if (ms && typeof ms.getText === 'function') {
        const data = await ms.getText(`reports/${name}`)
        return data == null ? { notFound: true } : { data }
      }
      const r = await fetch(`${base}/reports/${name}`, { headers, cache: 'no-store' })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.text() }
    } catch {
      return { error: 0 }
    }
  }

  // Enumerate reports through the runtime's typed listing (offline-capable).
  // storage.list(prefix) pages the server when reachable and ELSE derives the
  // listing from the read-through cache (the paths this app has read) overlaid
  // with the outbox — so the date list survives a network outage instead of
  // collapsing to empty. It returns the entries ARRAY directly ([] for an
  // empty/unknown dir), so a bridge present means we never special-case the
  // cursor. Standalone (no bridge) keeps the raw cursor walk.
  function datesFromEntries(entries) {
    const out = []
    for (const e of entries || []) {
      if (e.type === 'file' && typeof e.name === 'string' && e.name.endsWith('.html')) {
        out.push(e.name.slice(0, -'.html'.length))
      }
    }
    // ISO date names sort lexicographically = chronologically; newest first.
    out.sort((a, b) => (a < b ? 1 : a > b ? -1 : 0))
    return out
  }

  async function listReportDates() {
    if (ms && typeof ms.list === 'function') {
      try {
        const entries = await ms.list('reports/')
        return { dates: datesFromEntries(entries) }
      } catch {
        return { error: 0 }
      }
    }
    // Standalone fallback: walk the listing endpoint's cursor by hand. A
    // non-advancing cursor is treated as a server fault rather than spinning.
    const out = []
    let cursor = null
    try {
      for (let guard = 0; guard < 50; guard++) {
        const url = `${listBase}/reports/`
          + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : '')
        const r = await fetch(url, { headers })
        if (r.status === 404) return { dates: [] } // dir not created yet = empty
        if (!r.ok) return { error: r.status }
        const data = await r.json()
        for (const e of data.entries || []) {
          if (e.type === 'file' && typeof e.name === 'string' && e.name.endsWith('.html')) {
            out.push(e.name.slice(0, -'.html'.length))
          }
        }
        const prev = cursor
        cursor = data.next_cursor
        if (!cursor) break
        if (cursor === prev) return { error: -1 } // server returned same page
      }
    } catch {
      return { error: 0 }
    }
    out.sort((a, b) => (a < b ? 1 : a > b ? -1 : 0))
    return { dates: out }
  }

  // Subscribe to a JSON path through window.mobius.storage.subscribe so the
  // report LIST repaints live when an agent/cron writes a new reflection. The
  // runtime exposes per-path subscription (no directory watch), and the nightly
  // run rewrites state.json (last_run + streak) on every pass it lands a brief
  // — so a change there is the reliable "a new brief just landed" signal the
  // list re-lists on. Returns an unsubscribe fn, or a no-op when the runtime
  // bridge is absent (standalone).
  function subscribeJSON(path, cb) {
    if (ms && typeof ms.subscribe === 'function') {
      try { return ms.subscribe(path, cb) } catch { return () => {} }
    }
    return () => {}
  }

  return { getJSON, putJSON, getReportHtml, listReportDates, subscribeJSON }
}

// ---------------------------------------------------------------------------
// Offline reads are now served by the RUNTIME's read-through cache, not a
// hand-rolled localStorage snapshot. Every read goes through window.mobius.
// storage (get/getText/list), which mirrors values into IndexedDB on the
// online read and replays them when offline — so the dates list, the streak
// (state.json), and each opened brief body all survive a network outage from
// one source of truth. The old `reflection:<appId>:list` localStorage mirror
// was removed: it duplicated the runtime cache, could only ever hold list
// metadata (never the bodies, which is why briefs couldn't open offline), and
// drifted from the authoritative cache. No app-owned offline mirror remains.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Online/offline hook — runtime signal if present, else navigator.onLine.


export function useOnline() {
  const initial = (() => {
    if (typeof window === 'undefined') return true
    if (typeof window.mobius?.online === 'boolean') return window.mobius.online
    return navigator.onLine !== false
  })()
  const [online, setOnline] = useState(initial)
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    let unsub = null
    if (window.mobius && typeof window.mobius.onChange === 'function') {
      unsub = window.mobius.onChange((s) => {
        if (typeof s?.online === 'boolean') setOnline(s.online)
      })
    }
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
      if (unsub) unsub()
    }
  }, [])
  return online
}

export function chatOpenKey(appId) { return `rf:${appId}:chat-open:v${CHAT_OPEN_VERSION}` }
export function chatRatioKey(appId) { return `rf:${appId}:chat-ratio:v${CHAT_RATIO_VERSION}` }

export function readChatOpen(appId) {
  if (typeof localStorage === 'undefined') return false
  return localStorage.getItem(chatOpenKey(appId)) === 'true'
}

export function readChatRatio(appId) {
  if (typeof localStorage === 'undefined') return 0.5
  const raw = Number(localStorage.getItem(chatRatioKey(appId)))
  if (!Number.isFinite(raw) || raw <= 0 || raw >= 1) return 0.5
  return Math.max(0.05, Math.min(0.95, raw))
}
