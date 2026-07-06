import React from 'react'
import { cronExitLabel } from '../domain.js'


// ---------------------------------------------------------------------------
// Last-night status row
// ---------------------------------------------------------------------------
//
// Reads GET /api/admin/activity?since=<48h> (same auth pattern as other owner
// calls in this app), filters to cron_outcome events with job=reflection, takes
// the most-recent one, and renders a compact status line.
// A failure outcome shows an "Investigate" button that posts moebius:new-chat
// with a draft asking the agent to read /data/cron-logs/reflection.log.

export function LastNightStatus({ token }) {
  const [state, setState] = React.useState({ phase: 'loading', exitCode: null, ts: null })

  React.useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const since = new Date(Date.now() - 48 * 3600 * 1000).toISOString()
        const res = await fetch(`/api/admin/activity?since=${encodeURIComponent(since)}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) { if (!cancelled) setState({ phase: 'unavailable' }); return }
        const text = await res.text()
        if (cancelled) return
        // activity endpoint returns JSONL or a JSON array — handle both.
        const lines = text.trim().split('\n').filter(Boolean)
        const events = []
        for (const line of lines) {
          try {
            const obj = JSON.parse(line)
            // The endpoint may return a JSON array on some versions.
            if (Array.isArray(obj)) { obj.forEach((e) => events.push(e)); continue }
            events.push(obj)
          } catch { continue }
        }
        // Find the most-recent cron_outcome for reflection.
        const reflection = events
          .filter((e) => e.ev === 'cron_outcome' && e.job === 'reflection')
          .sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
        if (reflection.length === 0) {
          setState({ phase: 'none' })
        } else {
          const latest = reflection[0]
          setState({ phase: 'ready', exitCode: latest.exit_code, ts: latest.ts })
        }
      } catch {
        setState({ phase: 'unavailable' })
      }
    })()
    return () => { cancelled = true }
  }, [token])

  const investigate = () => {
    const draft = [
      'Something went wrong with the Reflection cron job. Please investigate:',
      '',
      '1. Check /data/cron-logs/reflection.log for the most recent error',
      '2. Identify the root cause (lock, timeout, config, or agent error)',
      '3. Propose a fix or next steps',
    ].join('\n')
    window.parent.postMessage({ type: 'moebius:new-chat', draft }, window.location.origin)
  }

  if (state.phase === 'loading' || state.phase === 'unavailable') return null

  const isFail = state.phase === 'ready' && Number(state.exitCode) !== 0 && Number(state.exitCode) !== 5
  const isSkip = state.phase === 'ready' && Number(state.exitCode) === 5
  const isNone = state.phase === 'none'
  const isOk   = state.phase === 'ready' && Number(state.exitCode) === 0

  const dotClass = isOk ? 'ok' : isFail ? 'fail' : isSkip ? 'skip' : 'none'
  const label = isNone
    ? 'No run recorded in the last 48 hours'
    : cronExitLabel(state.exitCode)
  const tsLabel = state.ts
    ? new Date(state.ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : ''

  return (
    <div className="rf-status-row">
      <span className={`rf-status-dot ${dotClass}`} aria-hidden="true" />
      <span className="rf-status-label">{label}</span>
      {tsLabel && <span className="rf-status-hint">{tsLabel}</span>}
      {isFail && (
        <button className="rf-status-investigate rf-pressable" onClick={investigate}>
          Investigate
        </button>
      )}
    </div>
  )
}
