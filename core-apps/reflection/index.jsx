// Reflection — thin app shell. The module tree is declared in mobius.json's
// source_files; the multi-file installer fetches each path and esbuild bundles
// from this entry, resolving the relative imports below at compile time.
//
//   constants.js  — shared scalar tables, report template blocks, and chat sizing constants
//   theme.js      — the single app stylesheet (CSS)
//   domain.js     — pure + DOM-level report, schedule, date, and split helpers
//   providers.js  — provider/model API loading helpers
//   storage.js    — storage layer, online signal, and chat split persistence keys
//   ui/*.jsx      — one React component per file
//
// Only App lives here: it owns top-level tab/detail state, persistence wiring,
// app-ready/dead-letter signals, and mounts the report/settings UI.
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { CSS } from './theme.js'
import { makeStorage, useOnline } from './storage.js'
import { LastNightStatus } from './ui/LastNightStatus.jsx'
import { ReportDetail } from './ui/ReportDetail.jsx'
import { ReportsList } from './ui/ReportsList.jsx'
import { SettingsTab } from './ui/SettingsTab.jsx'

export {
  extractReportQuestions,
  hardenReportHtml,
  isDarkColor,
  reportThemeStyle,
  sanitizeQuestions,
} from './domain.js'
export { makeStorage } from './storage.js'

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App({ appId, token }) {
  const [tab, setTab] = useState('reports')
  const [openDate, setOpenDate] = useState(null)
  const detailNavRef = useRef(null)
  const online = useOnline()
  const storage = useMemo(() => makeStorage(appId, token), [appId, token])
  const appReadyFiredRef = useRef(false)
  // A save can resolve 'queued' (durably outboxed offline) and then be FATALLY
  // refused later, when the outbox drains — an async outcome the resolved
  // promise at the call site can never carry. onDeadLetter is that out-of-band
  // channel: it fires once per such write so a "Saved" the user already saw is
  // honestly retracted here. Held at the app root because the originating
  // component (a question card, the settings form) is likely unmounted by drain
  // time. Replays unconsumed dead-letters on subscribe, so a refusal that
  // landed while the app was closed still surfaces on next open.
  const [deadLetter, setDeadLetter] = useState(null)
  useEffect(() => {
    if (!window.mobius || typeof window.mobius.onDeadLetter !== 'function') return undefined
    return window.mobius.onDeadLetter((dl) => {
      setDeadLetter(dl && dl.path === 'settings.json'
        ? 'Your schedule didn’t save — it was refused after going offline. Reopen Settings and save again.'
        : 'A queued change couldn’t be saved after you reconnected. Please try again.')
    })
  }, [])

  // Surface the streak in the header on the reports tab. The read below goes
  // through the runtime read-through cache (offline-capable), so the badge
  // fills from the last-known state.json even before the list finishes its own
  // load — and offline too. The list keeps its own authoritative copy.
  const [headerStreak, setHeaderStreak] = useState(0)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await storage.getJSON('state.json')
      if (cancelled) return
      if (res.data && Number.isFinite(res.data.streak)) {
        setHeaderStreak(res.data.streak)
      }
      // app_ready fires once after the initial state load (whether empty or not).
      if (!appReadyFiredRef.current) {
        appReadyFiredRef.current = true
        window.mobius?.signal?.('app_ready')
      }
    })()
    return () => { cancelled = true }
  }, [storage, appId, token])

  const closeDetail = useCallback(() => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    setOpenDate(null)
  }, [])

  const openDetail = useCallback(async (dateStr) => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    if (window.mobius?.nav?.open) {
      const handle = window.mobius.nav.open('reflection-report', () => {
        detailNavRef.current = null
        setOpenDate(null)
      })
      detailNavRef.current = handle
      await handle.ready?.catch(() => false)
      if (detailNavRef.current !== handle) return
    }
    window.mobius?.signal?.('brief_opened', { date: dateStr })
    setOpenDate(dateStr)
  }, [appId, token])

  useEffect(() => () => {
    try { detailNavRef.current?.close?.() } catch {}
  }, [])

  return (
    <div className="rf-root">
      <style>{CSS}</style>
      <div className="rf-aurora" aria-hidden="true" />
      <div className="rf-header">
        <div className="rf-brand">
          {/* Brand mark: the app's real glossy icon (downscaled + cached),
              no name text. Falls back to an accent dot when this install
              has no custom icon and the route 404s. */}
          <img
            src={`/api/apps/${appId}/icon?size=64`}
            alt=""
            width={26}
            height={26}
            className="rf-brand-icon"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
              const f = e.currentTarget.nextElementSibling
              if (f) f.style.display = 'flex'
            }}
          />
          <span className="rf-brand-fallback" style={{ display: 'none' }} aria-hidden="true">·</span>
        </div>
        <div className="rf-header-right">
          {headerStreak >= 1 && (
            <span className="rf-streak-badge" title={`${headerStreak} mornings in a row`}>
              <span aria-hidden="true">🔥</span>
              {headerStreak}
            </span>
          )}
          <div className="rf-seg" role="tablist" aria-label="View">
            <button
              role="tab"
              aria-selected={tab === 'reports'}
              className={`rf-seg-btn${tab === 'reports' ? ' is-active' : ''}`}
              onClick={() => setTab('reports')}
            >
              Briefs
            </button>
            <button
              role="tab"
              aria-selected={tab === 'settings'}
              className={`rf-seg-btn${tab === 'settings' ? ' is-active' : ''}`}
              onClick={() => { closeDetail(); setTab('settings') }}
            >
              Settings
            </button>
          </div>
        </div>
      </div>
      <div className="rf-divider" />
      <div className="rf-scroll">
        {deadLetter && (
          <div className="rf-deadletter" role="alert">
            <span>{deadLetter}</span>
            <button
              type="button"
              className="rf-deadletter__x rf-pressable"
              aria-label="Dismiss"
              onClick={() => setDeadLetter(null)}
            >
              ×
            </button>
          </div>
        )}
        {tab === 'reports' ? (
          <>
            {/* Last-night status row — shows most recent cron_outcome for reflection */}
            <LastNightStatus token={token} />
            <ReportsList
              appId={appId}
              storage={storage}
              online={online}
              onOpen={openDetail}
            />
            {openDate && (
              <ReportDetail
                dateStr={openDate}
                storage={storage}
                online={online}
                onBack={closeDetail}
                appId={appId}
                token={token}
              />
            )}
          </>
        ) : (
          <SettingsTab appId={appId} storage={storage} token={token} />
        )}
      </div>
    </div>
  )
}
