// Minimal client-side error telemetry. A broken state must be DIAGNOSABLE
// rather than a silent white screen — in the spirit of the recovery-over-
// prevention model. recordClientError does three things, each best-effort and
// non-throwing: logs to console for live devtools, keeps a small ring buffer
// of recent errors in sessionStorage (so the recovery surface + the owner can
// answer "what just broke?"), and POSTs to /api/client-error so uncaught SHELL
// errors land in the activity log as `app_error` events (no app_id == shell;
// the nightly Reflection digest reads these). The POST is standalone here — no
// api/client.js import — so this leaf logger can never cause an import cycle
// or route through apiFetch's 401-reload path, and a failed report can never
// itself throw.

const RING_KEY = 'mobius:error-log' // ring buffer of the last MAX errors
const MAX = 10

// Deployment-prefix-aware base, mirroring api/client.js (kept inline so this
// leaf module imports nothing). Empty string at the root, e.g. "/proxy/8001".
const BASE = (import.meta.env?.BASE_URL || '/').replace(/\/$/, '')

// Per-message 60s debounce so a render loop can't storm the network before the
// server's own debounce collapses the duplicates. Mirrors app-frame.html's
// reportAppError for the in-iframe path.
const _reportSeen = new Map()

/**
 * POST one shell error to /api/client-error → an `app_error` activity event
 * (the owner JWT carries no app_id, so it reads as a shell error). No-op
 * before login (no token); debounced per message. (See the file header for
 * why this is standalone + keepalive + swallow-all.)
 */
function postClientError(record) {
  let token
  try { token = localStorage.getItem('token') } catch { token = null }
  if (!token || !record.message) return
  const key = String(record.message).slice(0, 200)
  const now = Date.now()
  const last = _reportSeen.get(key)
  if (last && now - last < 60000) return
  _reportSeen.set(key, now)
  try {
    const detail = record.stack || record.componentStack
    fetch(`${BASE}/api/client-error`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        message: String(record.message).slice(0, 2000),
        where: record.where,
        stack: detail ? String(detail).slice(0, 8000) : undefined,
        url: (typeof location !== 'undefined') ? location.href : undefined,
      }),
      keepalive: true,
    }).catch(() => {})
  } catch {
    /* never throw from the error path */
  }
}

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
  // Additive remote sink: surface uncaught shell errors in the activity log.
  // After the console + ring writes so the diagnosable-locally behavior is
  // untouched even if the POST path is a no-op (pre-login) or fails.
  postClientError(record)
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

// Browser extensions (MetaMask, password managers, ad blockers, ...) inject
// scripts into the page and sometimes throw or reject on their own, for
// reasons that have nothing to do with the shell. Recognize their own
// script/stack origin and skip it here too, so it never becomes a fake
// "app_error" activity event (and, worse, a "the app crashed" chat) for
// something Möbius code never touched. Mirrors the same filter in
// app-frame.html's per-iframe handlers.
const EXTENSION_ORIGIN_RE = /\b(chrome|moz|safari-web|ms-browser)-extension:\/\//i
function isExtensionError(source, stack) {
  return EXTENSION_ORIGIN_RE.test(String(source || '')) || EXTENSION_ORIGIN_RE.test(String(stack || ''))
}

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
    if (isExtensionError(e.filename, e.error?.stack)) {
      console.warn('[mobius] ignored browser-extension error (not the shell):', e.message)
      return
    }
    recordClientError({
      where: 'window.onerror',
      message: e.message,
      error: e.error,
      stack: e.error?.stack,
    })
  })

  window.addEventListener('unhandledrejection', (e) => {
    const reason = e.reason
    if (isExtensionError('', reason?.stack)) {
      console.warn('[mobius] ignored browser-extension rejection (not the shell):', reason?.message ?? reason)
      return
    }
    recordClientError({
      where: 'unhandledrejection',
      message: reason?.message ?? String(reason),
      error: reason,
      stack: reason?.stack,
    })
  })
}
