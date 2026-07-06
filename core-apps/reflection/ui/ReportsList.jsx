import { useEffect, useRef, useState } from 'react'
import { dayOfMonth, relativeLabel, subLabel, weekdayInitial } from '../domain.js'
import { StreakBar } from './StreakBar.jsx'

// ---------------------------------------------------------------------------
// Reports list
// ---------------------------------------------------------------------------

export function ReportsList({ appId, storage, online, onOpen }) {
  const [dates, setDates] = useState([])
  const [streak, setStreak] = useState(0)
  const [lastSummary, setLastSummary] = useState('')
  const [phase, setPhase] = useState('loading') // loading | ready | error
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setPhase((p) => (dates.length ? p : 'loading'))
      // Both reads are served by the runtime read-through cache, so offline
      // returns the last-known listing + state automatically — no separate
      // app-owned snapshot to consult or keep in sync.
      const [listRes, stateRes] = await Promise.all([
        storage.listReportDates(),
        storage.getJSON('state.json'),
      ])
      if (cancelled) return

      // state.json: notFound is normal (cron hasn't written it) -> streak 0.
      // Offline with a prior read, getJSON resolves the cached value; a true
      // error (standalone fetch failure) leaves the header at zero.
      let nextStreak = 0
      let nextSummary = ''
      if (stateRes.data && typeof stateRes.data === 'object') {
        nextStreak = Number.isFinite(stateRes.data.streak) ? stateRes.data.streak : 0
        nextSummary = typeof stateRes.data.last_summary === 'string' ? stateRes.data.last_summary : ''
      }
      setStreak(nextStreak)
      setLastSummary(nextSummary)

      if (listRes.dates) {
        // list() returns an array whether online (server-authoritative) or
        // offline (derived from the read-through cache) — trust it either way.
        setDates(listRes.dates)
        setPhase('ready')
      } else if (dates.length) {
        // Standalone listing failed but we already have rows on screen — keep
        // them rather than blanking to an error.
        setPhase('ready')
      } else {
        // Listing failed and nothing to show — surface a retryable error.
        setPhase('error')
      }
    })()
    return () => { cancelled = true }
    // reloadKey forces a manual retry; dates intentionally excluded so a
    // state update inside the effect doesn't re-trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, storage, reloadKey])

  // Live refresh: a new brief written while the app is open should appear
  // without a manual "Try again" or a full iframe reload. The cron updates
  // state.json (last_run/streak) on every overnight pass, so a change there is
  // the signal that a new brief just landed — re-list when it fires. subscribe
  // delivers the current value immediately on register; we skip that first
  // synchronous fire so we don't double-load right after the mount effect.
  useEffect(() => {
    let primed = false
    const unsub = storage.subscribeJSON('state.json', () => {
      if (!primed) { primed = true; return }
      setReloadKey((k) => k + 1)
    })
    return () => { try { unsub && unsub() } catch {} }
  }, [storage])

  // Reconnecting after an offline stretch should re-list (the offline view is
  // a frozen cached snapshot; tonight's run only appears after a fresh
  // listing). Bump reloadKey on the false→true transition.
  const wasOnline = useRef(online)
  useEffect(() => {
    if (online && !wasOnline.current) setReloadKey((k) => k + 1)
    wasOnline.current = online
  }, [online])

  if (phase === 'loading' && dates.length === 0) {
    return (
      <div className="rf-loading-wrap">
        <span className="rf-spinner" aria-hidden="true" />
        <div>Gathering last night’s brief…</div>
      </div>
    )
  }

  if (phase === 'error' && dates.length === 0) {
    return (
      <div className="rf-error-box">
        <span>
          {online
            ? 'Couldn’t load your briefs just now.'
            : 'You’re offline and there’s nothing cached yet.'}
        </span>
        {online && (
          <button className="rf-retry-btn rf-pressable" onClick={() => setReloadKey((k) => k + 1)}>
            Try again
          </button>
        )}
      </div>
    )
  }

  if (dates.length === 0) {
    return (
      <div className="rf-empty">
        <div className="rf-empty-mark">
          <span className="rf-empty-mark-glyph" aria-hidden="true">🌙</span>
        </div>
        <div className="rf-empty-title">No briefs yet</div>
        Reflection runs overnight — consolidating what the day’s agents learned,
        tidying your Memory, and tending your apps. Your first morning brief will
        be waiting right here.
      </div>
    )
  }

  return (
    <div className="rf-rise">
      <StreakBar streak={streak} />
      {!online && (
        <div className="rf-offline-banner">
          <span aria-hidden="true">🌙</span>
          Offline — showing your last cached briefs. Tonight’s brief appears
          once you’re back online.
        </div>
      )}
      <div className="rf-list">
        {dates.map((d, i) => (
          <button
            key={d}
            className={`rf-card${i === 0 ? ' is-latest' : ''}`}
            onClick={() => onOpen(d)}
          >
            <div className={`rf-date-tile${i === 0 ? ' is-latest' : ''}`} aria-hidden="true">
              <span className="rf-date-tile-day">{weekdayInitial(d)}</span>
              <span className="rf-date-tile-num">{dayOfMonth(d)}</span>
            </div>
            <div className="rf-card-main">
              <div className="rf-card-label-row">
                <span className="rf-card-label">{relativeLabel(d)}</span>
                {i === 0 && <span className="rf-latest-pill">Latest</span>}
              </div>
              <span className="rf-card-sub">{subLabel(d)}</span>
              {i === 0 && lastSummary && (
                <span className="rf-card-tldr">{lastSummary}</span>
              )}
            </div>
            <span className="rf-card-chevron" aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </div>
  )
}
