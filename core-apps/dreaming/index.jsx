/* Dreaming — the nightly morning-brief viewer. Lists dated reports the dreaming agent leaves overnight, opens each as a sandboxed iframe, tracks a streak, and lets the owner pick the run hour + verbosity. */
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const VERBOSITY_OPTIONS = [
  { id: 'terse', label: 'Terse', hint: 'A short paragraph — just the highlights.' },
  { id: 'standard', label: 'Standard', hint: 'A few paragraphs, a suggestion or two, one closing thought.' },
  { id: 'chatty', label: 'Chatty', hint: 'A longer narrative with more pattern-spotting.' },
]
const DEFAULT_VERBOSITY = 'standard'

// The default schedule: 06:00 local -> "0 6 * * *". We only ever let the
// user pick an hour (minute pinned to 0), so the cron field is always
// "0 <h> * * *". parseCronHour / buildCron are the single round-trip
// between the picker's hour and that string.
const DEFAULT_HOUR = 6
const DEFAULT_CRON = '0 6 * * *'

// Pull the hour out of a "0 <h> * * *" cron string. Returns null for
// anything that doesn't match the minute-0, single-hour shape this app
// writes — a hand-edited cron (e.g. "30 6 * * *" or "0 6,18 * * *")
// shouldn't be silently coerced to an hour the picker can't represent;
// the caller falls back to the default and the UI notes it can't show
// a custom schedule rather than lying about one.
function parseCronHour(cron) {
  if (typeof cron !== 'string') return null
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return null
  const [min, hr, dom, mon, dow] = parts
  if (min !== '0' || dom !== '*' || mon !== '*' || dow !== '*') return null
  if (!/^\d{1,2}$/.test(hr)) return null
  const h = Number(hr)
  if (h < 0 || h > 23) return null
  return h
}

function buildCron(hour) {
  return `0 ${hour} * * *`
}

// "0 6 * * *" -> "06:00" for the <input type="time"> value.
function hourToTimeValue(hour) {
  return `${String(hour).padStart(2, '0')}:00`
}

// ---------------------------------------------------------------------------
// Date helpers — report names are <YYYY-MM-DD>.
// ---------------------------------------------------------------------------

function todayLocalDateStr() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function yesterdayLocalDateStr() {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

// Relative label for a card: "Today" / "Yesterday" / full date.
function relativeLabel(dateStr) {
  if (dateStr === todayLocalDateStr()) return 'Today'
  if (dateStr === yesterdayLocalDateStr()) return 'Yesterday'
  // Anchor at noon so the date doesn't slip a day under a timezone offset.
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric',
  })
}

// The smaller line under the relative label — full date for Today/Yesterday,
// and the year for older cards (so a card from a different year isn't
// ambiguous).
function subLabel(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return ''
  const rel = relativeLabel(dateStr)
  if (rel === 'Today' || rel === 'Yesterday') {
    return d.toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })
  }
  return d.toLocaleDateString(undefined, { year: 'numeric' })
}

// ---------------------------------------------------------------------------
// Styles. All structural colors are CSS variables so light + dark both work;
// the app-specific accent (the dream-purple) is the one committed hardcode.
// ---------------------------------------------------------------------------

const ACCENT = '#7c6cf0'        // dreaming's own violet, used for moon/flame glyphs
const ACCENT_DIM = 'rgba(124,108,240,0.14)'

const S = {
  root: {
    height: '100%', display: 'flex', flexDirection: 'column',
    background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'var(--font)', maxWidth: '100%', overflowX: 'hidden',
  },
  header: {
    padding: '20px 20px 0', display: 'flex', alignItems: 'center',
    justifyContent: 'space-between', flexShrink: 0, gap: '12px',
    flexWrap: 'wrap',
  },
  titleRow: { display: 'flex', alignItems: 'center', gap: '10px', minWidth: 0 },
  moon: {
    fontSize: '22px', lineHeight: 1, filter: 'saturate(1.1)',
  },
  title: {
    fontSize: '23px', fontWeight: 700, letterSpacing: '-0.4px', margin: 0,
  },
  streakBadge: (quiet) => ({
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '4px 11px', borderRadius: '999px',
    background: quiet ? 'var(--surface)' : ACCENT_DIM,
    color: quiet ? 'var(--muted)' : ACCENT,
    border: `1px solid ${quiet ? 'var(--border)' : 'transparent'}`,
    fontSize: '12.5px', fontWeight: 600, lineHeight: 1.2, whiteSpace: 'nowrap',
  }),
  tabs: {
    display: 'flex', gap: '2px', background: 'var(--surface)',
    borderRadius: '9px', padding: '3px', border: '1px solid var(--border)',
  },
  tab: (active) => ({
    padding: '6px 15px', borderRadius: '6px', border: 'none', cursor: 'pointer',
    fontSize: '13px', fontWeight: 600,
    background: active ? ACCENT : 'transparent',
    color: active ? '#fff' : 'var(--muted)',
    transition: 'background 0.15s, color 0.15s',
    fontFamily: 'var(--font)',
  }),
  divider: { height: '1px', background: 'var(--border)', margin: '16px 20px 0' },
  scroll: {
    flex: 1, overflowY: 'auto', overflowX: 'hidden',
    padding: '16px 20px 36px',
    wordBreak: 'break-word', overflowWrap: 'anywhere',
  },

  // Reports list
  list: { display: 'flex', flexDirection: 'column', gap: '11px', maxWidth: '640px', margin: '0 auto' },
  card: (latest) => ({
    display: 'flex', alignItems: 'stretch', gap: '14px',
    width: '100%', textAlign: 'left',
    border: '1px solid var(--border)', borderRadius: '14px',
    background: 'var(--surface)', padding: '15px 17px',
    cursor: 'pointer', color: 'var(--text)', fontFamily: 'var(--font)',
    transition: 'border-color 0.15s, transform 0.08s',
    position: 'relative', overflow: 'hidden',
    borderLeft: latest ? `3px solid ${ACCENT}` : '1px solid var(--border)',
  }),
  cardMain: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '3px' },
  cardLabel: { fontSize: '16px', fontWeight: 700, letterSpacing: '-0.2px', lineHeight: 1.25 },
  cardSub: { fontSize: '12px', color: 'var(--muted)', fontWeight: 500 },
  cardTldr: {
    fontSize: '13px', color: 'var(--muted)', lineHeight: 1.5, marginTop: '6px',
    display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
  },
  cardChevron: {
    alignSelf: 'center', fontSize: '18px', color: 'var(--muted)',
    flexShrink: 0, lineHeight: 1,
  },
  latestPill: {
    alignSelf: 'flex-start', marginTop: '8px',
    fontSize: '10.5px', fontWeight: 700, letterSpacing: '0.6px',
    textTransform: 'uppercase', color: ACCENT,
    background: ACCENT_DIM, padding: '2px 8px', borderRadius: '999px',
  },

  empty: {
    textAlign: 'center', padding: '64px 24px', color: 'var(--muted)',
    fontSize: '14px', lineHeight: 1.65, maxWidth: '420px', margin: '0 auto',
  },
  emptyMoon: { fontSize: '40px', display: 'block', marginBottom: '14px', opacity: 0.85 },
  loading: { textAlign: 'center', padding: '64px 24px', color: 'var(--muted)', fontSize: '13px' },
  errorBox: {
    maxWidth: '640px', margin: '0 auto', padding: '14px 16px',
    borderRadius: '12px', border: '1px solid var(--border)',
    background: 'var(--surface)', color: 'var(--text)', fontSize: '13px',
    lineHeight: 1.55, display: 'flex', flexDirection: 'column', gap: '10px',
  },
  retryBtn: {
    alignSelf: 'flex-start', padding: '6px 13px', borderRadius: '8px',
    border: `1px solid ${ACCENT}`, background: 'transparent', color: ACCENT,
    fontSize: '12.5px', fontWeight: 600, cursor: 'pointer', fontFamily: 'var(--font)',
  },
  offlineBanner: {
    maxWidth: '640px', margin: '0 auto 14px', padding: '9px 13px',
    borderRadius: '10px', background: ACCENT_DIM, border: '1px solid var(--border)',
    color: 'var(--text)', fontSize: '12.5px', lineHeight: 1.45,
  },

  // Report detail
  detail: { position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', background: 'var(--bg)' },
  detailBar: {
    display: 'flex', alignItems: 'center', gap: '12px',
    padding: '12px 16px', borderBottom: '1px solid var(--border)',
    flexShrink: 0, background: 'var(--surface)',
  },
  backBtn: {
    display: 'inline-flex', alignItems: 'center', gap: '6px',
    padding: '7px 13px 7px 10px', borderRadius: '9px',
    border: '1px solid var(--border)', background: 'var(--bg)',
    color: 'var(--text)', fontSize: '13px', fontWeight: 600,
    cursor: 'pointer', fontFamily: 'var(--font)', flexShrink: 0,
  },
  detailTitle: { display: 'flex', flexDirection: 'column', minWidth: 0, lineHeight: 1.25 },
  detailTitleMain: { fontSize: '15px', fontWeight: 700, letterSpacing: '-0.2px' },
  detailTitleSub: { fontSize: '11.5px', color: 'var(--muted)', fontWeight: 500 },
  iframe: { flex: 1, width: '100%', border: 'none', background: 'var(--bg)' },
  detailLoading: {
    position: 'absolute', inset: 0, top: '53px',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    color: 'var(--muted)', fontSize: '13px', background: 'var(--bg)',
  },

  // Settings
  settingsWrap: { maxWidth: '560px', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: '26px' },
  section: { display: 'flex', flexDirection: 'column', gap: '8px' },
  sectionLabel: { fontSize: '14px', fontWeight: 700, letterSpacing: '-0.1px', margin: 0 },
  note: { fontSize: '12.5px', color: 'var(--muted)', margin: 0, lineHeight: 1.55 },
  timeRow: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginTop: '2px' },
  timeInput: {
    padding: '8px 11px', fontSize: '15px', fontFamily: 'var(--font)',
    background: 'var(--surface)', color: 'var(--text)',
    border: '1px solid var(--border)', borderRadius: '9px',
    outline: 'none', width: '128px',
  },
  customCronNote: {
    fontSize: '12px', color: 'var(--muted)', lineHeight: 1.5,
    padding: '8px 11px', borderRadius: '9px',
    background: 'var(--surface)', border: '1px solid var(--border)', marginTop: '2px',
  },
  verbList: { display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '2px' },
  verbRow: (on) => ({
    display: 'flex', alignItems: 'flex-start', gap: '11px',
    padding: '11px 13px', borderRadius: '11px', cursor: 'pointer',
    background: on ? ACCENT_DIM : 'var(--surface)',
    border: `1px solid ${on ? ACCENT : 'var(--border)'}`,
    userSelect: 'none', transition: 'background 0.12s, border-color 0.12s',
  }),
  verbRadio: (on) => ({
    width: '16px', height: '16px', borderRadius: '999px', marginTop: '2px',
    border: `2px solid ${on ? ACCENT : 'var(--muted)'}`,
    background: 'transparent', flexShrink: 0, position: 'relative',
    boxShadow: on ? `inset 0 0 0 3px ${ACCENT}` : 'none',
  }),
  verbMain: { display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0 },
  verbTitle: { fontSize: '13.5px', fontWeight: 600 },
  verbHint: { fontSize: '12px', color: 'var(--muted)', lineHeight: 1.45 },
  saveRow: { display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', marginTop: '4px' },
  saveBtn: (busy) => ({
    padding: '9px 20px', borderRadius: '11px', border: 'none',
    background: busy ? 'var(--surface)' : ACCENT,
    color: busy ? 'var(--muted)' : '#fff',
    fontSize: '13.5px', fontWeight: 700, cursor: busy ? 'default' : 'pointer',
    fontFamily: 'var(--font)', transition: 'background 0.15s',
  }),
  toast: { fontSize: '12.5px', color: 'var(--green, #3fb950)', fontWeight: 600 },
  errorToast: { fontSize: '12.5px', color: 'var(--danger, #f85149)', fontWeight: 600 },
  scheduleHint: {
    fontSize: '12px', color: 'var(--muted)', lineHeight: 1.55,
    padding: '10px 13px', borderRadius: '10px',
    background: ACCENT_DIM, border: '1px solid var(--border)',
  },
}

// ---------------------------------------------------------------------------
// Storage — raw fetch with the app token (per the data contract). JSON paths
// parse JSON; report bodies are read as text. A real 404 (storage empty on
// first run) is normal and returns the `notFound` shape so callers can tell
// it apart from a network failure (`error`) and treat each correctly.
// ---------------------------------------------------------------------------

function makeStorage(appId, token) {
  const headers = { Authorization: `Bearer ${token}` }
  const base = `/api/storage/apps/${appId}`
  const listBase = `/api/storage/apps-list/${appId}`

  async function getJSON(path) {
    try {
      const r = await fetch(`${base}/${path}`, { headers })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.json() }
    } catch {
      return { error: 0 }
    }
  }

  async function putJSON(path, obj) {
    const r = await fetch(`${base}/${path}`, {
      method: 'PUT',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    })
    if (!r.ok) throw new Error(`PUT ${path} failed (${r.status})`)
    return true
  }

  async function getReportHtml(name) {
    // Report bodies are raw HTML documents — read as text, not JSON.
    try {
      const r = await fetch(`${base}/reports/${name}`, { headers })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.text() }
    } catch {
      return { error: 0 }
    }
  }

  // Enumerate reports via the listing endpoint (newest-first), walking the
  // cursor. A non-advancing cursor is treated as a server fault rather than
  // spinning forever. Returns { dates: [...] } on success, or { error } /
  // { notFound } so the caller can distinguish "no reports yet" from "the
  // listing call failed" and keep its cached snapshot in the latter case.
  async function listReportDates() {
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
    // ISO date names sort lexicographically = chronologically; newest first.
    out.sort((a, b) => (a < b ? 1 : a > b ? -1 : 0))
    return { dates: out }
  }

  return { getJSON, putJSON, getReportHtml, listReportDates }
}

// ---------------------------------------------------------------------------
// Tiny offline snapshot. Dreaming reads via direct fetch (text bodies +
// apps-list), neither of which the platform read-cache covers, so we keep our
// own localStorage snapshot: the dates list, the streak, and the latest
// summary. This is read-only mirror state — only the cron writes reports, so
// the snapshot just paints the same thing the user saw before they lost
// connectivity. Bodies aren't cached here (the report iframe re-fetches; if
// offline it shows a graceful error), only the cheap list metadata.
// ---------------------------------------------------------------------------

const CACHE_VERSION = 1
function cacheKey(appId) { return `dreaming:${appId}:list:v${CACHE_VERSION}` }

function readCache(appId) {
  try {
    const raw = localStorage.getItem(cacheKey(appId))
    if (!raw) return null
    const p = JSON.parse(raw)
    if (!p || typeof p !== 'object') return null
    return {
      dates: Array.isArray(p.dates) ? p.dates.filter((d) => typeof d === 'string') : [],
      streak: Number.isFinite(p.streak) ? p.streak : 0,
      lastSummary: typeof p.lastSummary === 'string' ? p.lastSummary : '',
      lastRun: typeof p.lastRun === 'string' ? p.lastRun : '',
    }
  } catch {
    return null
  }
}

function writeCache(appId, snap) {
  try {
    localStorage.setItem(cacheKey(appId), JSON.stringify(snap))
  } catch {
    // Quota / private-mode Safari: skip silently. In-memory state still works.
  }
}

// ---------------------------------------------------------------------------
// Online/offline hook — runtime signal if present, else navigator.onLine.
// ---------------------------------------------------------------------------

function useOnline() {
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

// ---------------------------------------------------------------------------
// Report detail — full HTML in a sandboxed iframe (srcDoc, no allow-scripts).
// The report is a complete static document with its own <style>; keeping
// scripts off is the containment. Back is wired through history.pushState +
// popstate so the phone's back gesture returns to the list (the app falls
// back to the in-bar back button when history is unavailable).
// ---------------------------------------------------------------------------

function ReportDetail({ dateStr, storage, online, onBack }) {
  const [state, setState] = useState({ phase: 'loading', html: '' })

  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading', html: '' })
    ;(async () => {
      const res = await storage.getReportHtml(`${dateStr}.html`)
      if (cancelled) return
      if (res.data != null) setState({ phase: 'ready', html: res.data })
      else if (res.notFound) setState({ phase: 'missing', html: '' })
      else setState({ phase: 'error', html: '' })
    })()
    return () => { cancelled = true }
  }, [dateStr, storage])

  // Push a history entry on mount so a back gesture pops it; intercept the
  // pop to return to the list instead of leaving the app. If the user backs
  // via the in-app button we go() to consume our own entry so we don't leave
  // a dangling forward state.
  const poppedRef = useRef(false)
  useEffect(() => {
    let pushed = false
    try {
      window.history.pushState({ dreamingDetail: dateStr }, '')
      pushed = true
    } catch {
      pushed = false
    }
    const onPop = () => {
      poppedRef.current = true
      onBack()
    }
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      // Unmounting because the user tapped the in-app back button (not a
      // browser pop): consume the entry we pushed so history stays clean.
      if (pushed && !poppedRef.current) {
        try { window.history.back() } catch { /* ignore */ }
      }
    }
  }, [dateStr, onBack])

  return (
    <div style={S.detail}>
      <div style={S.detailBar}>
        <button style={S.backBtn} onClick={onBack} aria-label="Back to reports">
          <span aria-hidden="true" style={{ fontSize: '15px' }}>‹</span> Back
        </button>
        <div style={S.detailTitle}>
          <span style={S.detailTitleMain}>{relativeLabel(dateStr)}'s brief</span>
          <span style={S.detailTitleSub}>{subLabel(dateStr)}</span>
        </div>
      </div>
      {state.phase === 'loading' && (
        <div style={{ ...S.detailLoading, position: 'relative', top: 0 }}>Opening your brief…</div>
      )}
      {state.phase === 'ready' && (
        <iframe
          style={S.iframe}
          title={`Morning brief for ${dateStr}`}
          srcDoc={state.html}
          // The report is static HTML/CSS authored by the agent. sandbox
          // WITHOUT allow-scripts is the containment: same-origin so its
          // own <style> + relative anchors resolve, but no script execution.
          sandbox="allow-same-origin"
        />
      )}
      {state.phase === 'missing' && (
        <div style={{ ...S.empty, paddingTop: '48px' }}>
          This brief is no longer available.
        </div>
      )}
      {state.phase === 'error' && (
        <div style={{ ...S.empty, paddingTop: '48px' }}>
          {online
            ? 'This brief could not be loaded. Try opening it again in a moment.'
            : 'You’re offline — open this brief again once you’re back online.'}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reports list
// ---------------------------------------------------------------------------

function ReportsList({ appId, storage, online, onOpen }) {
  const cached = useMemo(() => readCache(appId), [appId])
  const [dates, setDates] = useState(cached?.dates || [])
  const [streak, setStreak] = useState(cached?.streak || 0)
  const [lastSummary, setLastSummary] = useState(cached?.lastSummary || '')
  const [phase, setPhase] = useState('loading') // loading | ready | error
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setPhase((p) => (dates.length ? p : 'loading'))
      const [listRes, stateRes] = await Promise.all([
        storage.listReportDates(),
        storage.getJSON('state.json'),
      ])
      if (cancelled) return

      // state.json: 404 is normal (cron hasn't written it) -> streak 0.
      let nextStreak = 0
      let nextSummary = ''
      let nextLastRun = ''
      if (stateRes.data && typeof stateRes.data === 'object') {
        nextStreak = Number.isFinite(stateRes.data.streak) ? stateRes.data.streak : 0
        nextSummary = typeof stateRes.data.last_summary === 'string' ? stateRes.data.last_summary : ''
        nextLastRun = typeof stateRes.data.last_run === 'string' ? stateRes.data.last_run : ''
      } else if (stateRes.error != null && cached) {
        // Couldn't reach state.json (offline) — keep the cached header.
        nextStreak = cached.streak
        nextSummary = cached.lastSummary
      }
      setStreak(nextStreak)
      setLastSummary(nextSummary)

      if (listRes.dates) {
        // Listing succeeded (even if empty []): trust the server.
        setDates(listRes.dates)
        setPhase('ready')
        writeCache(appId, {
          dates: listRes.dates, streak: nextStreak,
          lastSummary: nextSummary, lastRun: nextLastRun,
        })
      } else if (cached && cached.dates.length) {
        // Listing failed but we have a cached snapshot — show it.
        setDates(cached.dates)
        setPhase('ready')
      } else {
        // Listing failed and nothing cached — surface a retryable error.
        setPhase('error')
      }
    })()
    return () => { cancelled = true }
    // reloadKey forces a manual retry; dates intentionally excluded so a
    // state update inside the effect doesn't re-trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, storage, reloadKey])

  if (phase === 'loading' && dates.length === 0) {
    return <div style={S.loading}>Gathering last night’s dream…</div>
  }

  if (phase === 'error' && dates.length === 0) {
    return (
      <div style={S.errorBox}>
        <span>
          {online
            ? 'Couldn’t load your briefs just now.'
            : 'You’re offline and there’s nothing cached yet.'}
        </span>
        {online && (
          <button style={S.retryBtn} onClick={() => setReloadKey((k) => k + 1)}>
            Try again
          </button>
        )}
      </div>
    )
  }

  if (dates.length === 0) {
    return (
      <div style={S.empty}>
        <span style={S.emptyMoon} aria-hidden="true">🌙</span>
        No reports yet — Dreaming runs overnight and leaves your first
        morning brief here.
      </div>
    )
  }

  return (
    <div>
      <StreakBar streak={streak} />
      {!online && (
        <div style={S.offlineBanner}>
          Offline — showing your last cached briefs. Tonight’s dream will
          appear once you’re back online.
        </div>
      )}
      <div style={S.list}>
        {dates.map((d, i) => (
          <button
            key={d}
            style={S.card(i === 0)}
            onClick={() => onOpen(d)}
            onMouseDown={(e) => { e.currentTarget.style.transform = 'scale(0.995)' }}
            onMouseUp={(e) => { e.currentTarget.style.transform = 'none' }}
            onMouseLeave={(e) => { e.currentTarget.style.transform = 'none' }}
          >
            <div style={S.cardMain}>
              <span style={S.cardLabel}>{relativeLabel(d)}</span>
              <span style={S.cardSub}>{subLabel(d)}</span>
              {i === 0 && lastSummary && (
                <span style={S.cardTldr}>{lastSummary}</span>
              )}
              {i === 0 && <span style={S.latestPill}>Latest</span>}
            </div>
            <span style={S.cardChevron} aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function StreakBar({ streak }) {
  if (!streak || streak < 1) return null
  return (
    <div style={{ maxWidth: '640px', margin: '0 auto 14px', display: 'flex' }}>
      <span style={S.streakBadge(false)}>
        <span aria-hidden="true">🔥</span>
        {streak}-day streak
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function SettingsTab({ appId, storage, online }) {
  const [hour, setHour] = useState(DEFAULT_HOUR)
  const [verbosity, setVerbosity] = useState(DEFAULT_VERBOSITY)
  const [excludeApps, setExcludeApps] = useState([])
  // The raw cron we loaded — when it's a custom shape parseCronHour can't
  // represent (a non-zero minute, multiple hours), we surface it read-only
  // rather than silently rewriting it to "0 <h> * * *" on the next save.
  const [rawCron, setRawCron] = useState(DEFAULT_CRON)
  const [cronIsCustom, setCronIsCustom] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await storage.getJSON('settings.json')
      if (cancelled) return
      const s = res.data && typeof res.data === 'object' ? res.data : null
      if (s) {
        const parsedHour = parseCronHour(s.cron)
        if (parsedHour != null) {
          setHour(parsedHour)
          setCronIsCustom(false)
        } else if (typeof s.cron === 'string' && s.cron.trim()) {
          // Hand-edited / multi-hour cron — keep it, show it read-only.
          setRawCron(s.cron)
          setCronIsCustom(true)
        }
        if (VERBOSITY_OPTIONS.some((v) => v.id === s.verbosity)) {
          setVerbosity(s.verbosity)
        }
        if (Array.isArray(s.exclude_apps)) setExcludeApps(s.exclude_apps)
      }
      // res.notFound (first run) -> keep the 06:00 / standard defaults.
      setLoading(false)
    })()
    return () => { cancelled = true }
  }, [storage])

  const onTimeChange = useCallback((e) => {
    // <input type="time"> can be cleared to "" -> NaN. Drop NaN so we never
    // write a corrupt cron; the input repaints with the last good value.
    const [hStr] = e.target.value.split(':')
    const h = Number(hStr)
    if (Number.isFinite(h) && h >= 0 && h <= 23) {
      setHour(h)
      setCronIsCustom(false) // editing the hour adopts the standard shape
    }
  }, [])

  const save = useCallback(async () => {
    if (saving) return
    setSaving(true)
    setError('')
    setToast('')
    // Preserve a custom cron verbatim if the user never touched the hour;
    // otherwise write the standard "0 <h> * * *".
    const cron = cronIsCustom ? rawCron : buildCron(hour)
    try {
      await storage.putJSON('settings.json', {
        cron,
        verbosity,
        exclude_apps: excludeApps,
      })
      setToast('Saved ✓')
      setTimeout(() => setToast(''), 2600)
    } catch {
      setError(online ? 'Could not save — try again.' : 'You’re offline — reconnect to save.')
    } finally {
      setSaving(false)
    }
  }, [saving, cronIsCustom, rawCron, hour, verbosity, excludeApps, storage, online])

  if (loading) return <div style={S.loading}>Loading settings…</div>

  return (
    <div style={S.settingsWrap}>
      <div style={S.section}>
        <h2 style={S.sectionLabel}>When to dream</h2>
        <p style={S.note}>
          Pick the hour your morning brief should be ready. Dreaming writes
          it overnight so it’s waiting when you wake up.
        </p>
        {cronIsCustom ? (
          <div style={S.customCronNote}>
            You have a custom schedule set (<code>{rawCron}</code>). Pick an
            hour below to switch to a simple daily time, or leave it as-is.
            <div style={{ ...S.timeRow, marginTop: '10px' }}>
              <input
                type="time"
                step="3600"
                style={S.timeInput}
                value={hourToTimeValue(hour)}
                onChange={onTimeChange}
                aria-label="Daily brief time"
              />
              <span style={S.note}>on the hour, every day</span>
            </div>
          </div>
        ) : (
          <div style={S.timeRow}>
            <input
              type="time"
              step="3600"
              style={S.timeInput}
              value={hourToTimeValue(hour)}
              onChange={onTimeChange}
              aria-label="Daily brief time"
            />
            <span style={S.note}>on the hour, every day</span>
          </div>
        )}
        <div style={S.scheduleHint}>
          Schedule changes take effect after the dreaming agent re-installs
          its overnight job — usually by the next run. The app saves your
          preference; the agent picks it up from there.
        </div>
      </div>

      <div style={S.section}>
        <h2 style={S.sectionLabel}>How much detail</h2>
        <p style={S.note}>
          How long and discursive each brief should be.
        </p>
        <div style={S.verbList} role="radiogroup" aria-label="Verbosity">
          {VERBOSITY_OPTIONS.map((opt) => {
            const on = verbosity === opt.id
            return (
              <div
                key={opt.id}
                style={S.verbRow(on)}
                onClick={() => setVerbosity(opt.id)}
                role="radio"
                aria-checked={on}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    setVerbosity(opt.id)
                  }
                }}
              >
                <span style={S.verbRadio(on)} aria-hidden="true" />
                <div style={S.verbMain}>
                  <span style={S.verbTitle}>{opt.label}</span>
                  <span style={S.verbHint}>{opt.hint}</span>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <div style={S.saveRow}>
        <button style={S.saveBtn(saving)} onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {toast && <span style={S.toast}>{toast}</span>}
        {error && <span style={S.errorToast}>{error}</span>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App({ appId, token }) {
  const [tab, setTab] = useState('reports')
  const [openDate, setOpenDate] = useState(null)
  const online = useOnline()
  const storage = useMemo(() => makeStorage(appId, token), [appId, token])

  // Surface the streak in the header on the reports tab. We read it once
  // here (cheap, cached) so the badge is present even before the list
  // finishes its own load. The list keeps its own authoritative copy.
  const [headerStreak, setHeaderStreak] = useState(() => readCache(appId)?.streak || 0)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await storage.getJSON('state.json')
      if (cancelled) return
      if (res.data && Number.isFinite(res.data.streak)) {
        setHeaderStreak(res.data.streak)
      }
    })()
    return () => { cancelled = true }
  }, [storage, appId])

  const closeDetail = useCallback(() => setOpenDate(null), [])

  return (
    <div style={S.root}>
      <div style={S.header}>
        <div style={S.titleRow}>
          <span style={S.moon} aria-hidden="true">🌙</span>
          <h1 style={S.title}>Dreaming</h1>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap' }}>
          {headerStreak >= 1 && (
            <span style={S.streakBadge(false)} title={`${headerStreak} mornings in a row`}>
              <span aria-hidden="true">🔥</span>
              {headerStreak}-day streak
            </span>
          )}
          <div style={S.tabs}>
            <button style={S.tab(tab === 'reports')} onClick={() => setTab('reports')}>
              Briefs
            </button>
            <button style={S.tab(tab === 'settings')} onClick={() => setTab('settings')}>
              Settings
            </button>
          </div>
        </div>
      </div>
      <div style={S.divider} />
      <div style={{ ...S.scroll, position: 'relative' }}>
        {tab === 'reports' ? (
          <>
            <ReportsList
              appId={appId}
              storage={storage}
              online={online}
              onOpen={setOpenDate}
            />
            {openDate && (
              <ReportDetail
                dateStr={openDate}
                storage={storage}
                online={online}
                onBack={closeDetail}
              />
            )}
          </>
        ) : (
          <SettingsTab appId={appId} storage={storage} online={online} />
        )}
      </div>
    </div>
  )
}
