import { useEffect, useState } from 'react'

// Single source of truth for connectivity.
//
// Why not just `navigator.onLine`: in a service-worker PWA the SW serves
// most requests from cache, so the browser rarely makes a real network
// attempt and its online/offline heuristic goes stale — `navigator.onLine`
// reports the PREVIOUS state until something forces an actual request to
// succeed or fail. Users saw the shell claim "online" while in airplane
// mode (and vice-versa, lagging by one transition). So `navigator.onLine`
// is treated as a HINT, not the truth.
//
// The truth comes from a real reachability probe: a `no-store` GET to
// `/api/health`, which the service worker deliberately does NOT cache (see
// sw.js), so it genuinely hits the network. Success = online, failure or
// timeout = offline.
//
// navigator.onLine is NOT trusted in either direction — it can read `false`
// even after the network is back (it only updates when a real request
// resolves, and the SW serves most requests from cache). So the probe always
// runs; the /api/health fetch is the sole source of truth. The window
// `offline` event still flips the UI to offline immediately (it's a prompt
// hint and the next probe confirms), but RECOVERY to online only ever comes
// from a successful probe — never from navigator.onLine going true.
//
// Re-probes on: window online/offline events, tab becoming visible, and a
// periodic interval while visible. Used by the chat composer (chat is
// online-only) and the shell's global offline indicator.

const HEALTH_URL = '/api/health'
const PROBE_TIMEOUT_MS = 4000
// Periodic re-probe while the tab is visible. Connectivity can change
// without any window event firing (captive portal, flaky mobile data), so
// we poll, but only when visible to avoid waking a backgrounded tab.
const POLL_INTERVAL_MS = 20000

async function probeReachable() {
  // ALWAYS probe — do not short-circuit on navigator.onLine. That flag is
  // stale in BOTH directions in a SW-served PWA: it can read `false` even
  // after the network is back (the browser only updates it when a real
  // request resolves, and the SW serves most requests from cache). An
  // earlier version returned false when navigator.onLine was false, which
  // wedged the indicator "offline" forever after reconnecting. The
  // /api/health fetch below is the only trustworthy signal: it actually
  // hits the network (the SW does not cache it), so success = truly online.
  let timer
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  try {
    if (ctrl) timer = setTimeout(() => ctrl.abort(), PROBE_TIMEOUT_MS)
    const res = await fetch(HEALTH_URL, {
      method: 'GET',
      cache: 'no-store',
      signal: ctrl ? ctrl.signal : undefined,
    })
    return res.ok
  } catch {
    // Network error, abort/timeout, or DNS failure → not reachable.
    return false
  } finally {
    if (timer) clearTimeout(timer)
  }
}

export default function useOnlineStatus() {
  // Seed optimistically from the hint so the UI doesn't flash "offline" on
  // a fast online load before the first probe resolves. The probe corrects
  // it within a few ms if wrong.
  const [online, setOnline] = useState(
    typeof navigator === 'undefined' ? true : navigator.onLine !== false,
  )

  useEffect(() => {
    let cancelled = false
    let inflight = false
    let lastLogged = null

    // TEMPORARY: log connectivity transitions to the same-origin ring buffer
    // the mini-app frame uses, so /diag.html shows shell + app events
    // together (confirms the offline-pill behaviour). Transition-only, so the
    // 20s poll doesn't spam it. Remove with the rest of the diag scaffolding.
    function logTransition(reachable, reason) {
      if (reachable === lastLogged) return
      lastLogged = reachable
      try {
        const key = 'mobius-diag-log'
        const arr = JSON.parse(localStorage.getItem(key) || '[]')
        arr.push({
          t: new Date().toISOString(),
          src: 'shell',
          online: typeof navigator !== 'undefined' ? navigator.onLine : null,
          tag: 'online=' + reachable,
          msg: reason + ' (navigator.onLine=' +
            (typeof navigator !== 'undefined' ? navigator.onLine : '?') + ')',
        })
        localStorage.setItem(key, JSON.stringify(arr.slice(-100)))
      } catch (e) { /* ignore */ }
    }

    async function check() {
      // Coalesce overlapping triggers (event + interval landing together).
      if (inflight) return
      inflight = true
      try {
        const reachable = await probeReachable()
        if (!cancelled) {
          logTransition(reachable, 'probe')
          setOnline(reachable)
        }
      } finally {
        inflight = false
      }
    }

    // A definite offline event is trustworthy — reflect it immediately
    // without waiting for a probe to time out.
    const onOffline = () => {
      if (!cancelled) { logTransition(false, 'offline-event'); setOnline(false) }
    }
    const onOnline = () => { check() }
    const onVisible = () => {
      if (document.visibilityState === 'visible') check()
    }

    window.addEventListener('online', onOnline)
    window.addEventListener('offline', onOffline)
    document.addEventListener('visibilitychange', onVisible)

    // Poll only while visible. The interval callback self-gates so a
    // backgrounded tab doesn't probe.
    const interval = setInterval(() => {
      if (document.visibilityState === 'visible') check()
    }, POLL_INTERVAL_MS)

    // Initial probe to correct the optimistic seed.
    check()

    return () => {
      cancelled = true
      clearInterval(interval)
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  return online
}
