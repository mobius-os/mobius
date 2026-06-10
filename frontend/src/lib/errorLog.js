// Minimal client-side error telemetry. There is no remote sink (Möbius is
// single-owner + self-hosted), but a broken state must be DIAGNOSABLE rather
// than a silent white screen — in the spirit of the recovery-over-prevention
// model. We keep a small ring buffer of the most recent errors in
// sessionStorage so the recovery surface (and the owner) can answer "what
// just broke?" instead of guessing. A real POST /api/debug/client-error sink
// can hook recordClientError later without touching any caller.

const RING_KEY = 'mobius:error-log' // ring buffer of the last MAX errors
const MAX = 10

/**
 * Records one client error: console for live devtools, plus a persisted ring
 * buffer + a single-most-recent entry. Storage failures are swallowed (the
 * console line still stands) so logging an error can never itself throw.
 */
export function recordClientError({ where, message, error, stack, componentStack } = {}) {
  const record = {
    where: where || 'unknown',
    message: String(message ?? error?.message ?? error ?? 'Unknown error'),
    stack: String(stack ?? error?.stack ?? '').slice(0, 2000),
    componentStack: String(componentStack ?? '').slice(0, 2000),
    at: new Date().toISOString(),
  }
  console.error(`[mobius:error:${record.where}]`, record.message)
  try {
    const ring = JSON.parse(sessionStorage.getItem(RING_KEY) || '[]')
    ring.push(record)
    while (ring.length > MAX) ring.shift()
    sessionStorage.setItem(RING_KEY, JSON.stringify(ring))
  } catch {
    /* storage full/disabled — the console line above is the fallback */
  }
}

/** Returns the recent-error ring (oldest first), or [] if unreadable. */
export function getRecentErrors() {
  try {
    return JSON.parse(sessionStorage.getItem(RING_KEY) || '[]')
  } catch {
    return []
  }
}

let installed = false

/**
 * Installs window-level handlers for the errors React's ErrorBoundary can't
 * catch: errors thrown in event handlers / async callbacks (window 'error')
 * and unhandled promise rejections. Idempotent.
 */
export function installGlobalErrorHandlers() {
  if (installed || typeof window === 'undefined') return
  installed = true

  window.addEventListener('error', (e) => {
    // Resource-load failures (an <img>/<script> 404) also fire 'error' but
    // carry no `error` object and aren't actionable script faults — skip them
    // so the log stays signal, not noise.
    if (!e.error && !e.message) return
    recordClientError({
      where: 'window.onerror',
      message: e.message,
      error: e.error,
      stack: e.error?.stack,
    })
  })

  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason
    recordClientError({
      where: 'unhandledrejection',
      message: reason?.message ?? String(reason),
      error: reason,
      stack: reason?.stack,
    })
  })
}
