import { useEffect, useState } from 'react'
import { resolveOnline } from '../lib/onlineStatus.js'

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
// `offline` event does NOT flip the UI to offline immediately — `onOffline`
// only schedules a deferred `check()` after OFFLINE_EVENT_GRACE_MS (see
// below) and never calls setOnline(false) itself. The UI transitions to
// offline only after that probe runs and the hysteresis verdict demotes.
// RECOVERY to online likewise only ever comes from a successful probe —
// never from navigator.onLine going true.
//
// The probe verdict is HYSTERETIC (see lib/onlineStatus.js): recovery to
// online is fast (one good probe clears offline) but demotion to offline is
// debounced (a streak of failed probes is required before flipping the banner,
// unless navigator.onLine itself reads false after the short offline-event
// grace below). This is what kills the spurious offline banner from one slow
// probe or a brief mobile-data↔Wi‑Fi handoff.
//
// Re-probes on: window online/offline events, tab becoming visible, and a
// periodic interval while visible. Used by the chat composer (chat is
// online-only) and the shell's global offline indicator.

const HEALTH_URL = '/api/health'
// 3s, not 2s. Online, /api/health answers in tens of ms, so 3s is a generous
// margin that also gives a COLD radio (first request after the screen wakes,
// or a backgrounded mobile tab returning to foreground) room to answer before
// we abort. An aborted probe counts as a failure, and while the failure-streak
// debounce (lib/onlineStatus.js) already prevents one slow probe from flipping
// the banner, widening the window means fewer cold-radio probes get aborted in
// the first place — belt and suspenders against the spurious-offline flap. The
// timeout only bites in the pathological Android case where an offline fetch
// hangs PENDING instead of failing fast (stale radio state) — there it caps how
// long an offline-capable mini-app waits for `online` to resolve false before
// it mounts with the owner token. The frame/module route itself is cache-first
// once warmed; this probe is now the remaining offline auth boundary. NOTE: we
// deliberately do NOT mount with the owner JWT BEFORE `online` resolves — that
// would put the long-lived owner JWT in the module URL during what might be a
// genuine online session (access-log exposure). Capping the probe is the safe
// lever; the JWT-in-URL boundary stays intact.
const PROBE_TIMEOUT_MS = 3000
// Android can emit a window `offline` event during a mobile-data → Wi‑Fi
// handoff even though the connection recovers a moment later. If we publish
// `offline` synchronously, the shell flashes the offline pill and the composer
// disables just as the user taps Send. Treat the event as a prompt to verify
// reachability after a short grace; a real offline state still appears quickly,
// while handoff blips never surface.
const OFFLINE_EVENT_GRACE_MS = 2500
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
  // Seed from navigator.onLine, treating it ASYMMETRICALLY (the key to fast
  // offline cold-open). The flag is unreliable only in the recovery direction:
  // it can read a stale `true` AFTER the network is back, OR (the bug we're
  // fixing) a stale `true` while genuinely offline on an Android PWA. But
  // `false` is NEVER a false negative — the browser does not claim offline when
  // a network exists. So:
  //   • navigator.onLine === false  → seed `false` and TRUST it. The app shows
  //     cached data instantly; no waiting on a multi-second probe that has to
  //     time out. (Previously we seeded `true` here and the app sat ~5s until
  //     the probe finally failed — the reported ~10s offline load.)
  //   • navigator.onLine !== false → seed `true` optimistically; the background
  //     probe demotes to offline within the probe window if it's wrong.
  // Promotion back to online ONLY ever comes from a successful /api/health
  // probe, never from the flag flipping true — that asymmetry is what kept the
  // earlier "wedged offline after reconnect" bug from recurring.
  const [online, setOnline] = useState(
    typeof navigator === 'undefined' ? true : navigator.onLine !== false,
  )

  useEffect(() => {
    let cancelled = false
    let inflight = false
    let offlineTimer = 0
    // Caller-held connectivity state threaded through resolveOnline between
    // check() calls. The streaks give the verdict its hysteresis:
    //   • successStreak — consecutive successful probes; gates promotion to
    //     online when navigator.onLine is stale-false (one Android offline
    //     false-positive stays offline; a real reconnect promotes on the 2nd).
    //   • failureStreak — consecutive failed probes; gates demotion to offline
    //     when navigator.onLine still reads online (one slow/aborted probe does
    //     not flip the banner; only a sustained failure does).
    //   • online — the current verdict, threaded so a sub-threshold probe holds
    //     the prior value instead of guessing.
    // Seeded from the same navigator.onLine asymmetry as the React state above.
    let connState = {
      successStreak: 0,
      failureStreak: 0,
      online: typeof navigator === 'undefined' ? true : navigator.onLine !== false,
    }

    // Compatibility: older service workers used this verdict to decide whether
    // offline-capable app code should go cache-first. Current SWs serve cached,
    // versioned app code cache-first in every connectivity state, but keeping
    // the post makes rolling updates safe for tabs still controlled by an older
    // worker.
    function postToSW(online) {
      try {
        navigator.serviceWorker?.controller?.postMessage({
          type: 'moebius:connectivity', online,
        })
      } catch (e) { /* no controller yet / unsupported — best effort only. */ }
    }

    function publish(next) {
      connState = { ...connState, online: next }
      setOnline(next)
      postToSW(next)
    }

    async function check() {
      // Coalesce overlapping triggers (event + interval landing together).
      if (inflight) return
      inflight = true
      try {
        const reachable = await probeReachable()
        if (cancelled) return
        const navOnLine = typeof navigator !== 'undefined' ? navigator.onLine : true
        const res = resolveOnline(reachable, navOnLine, connState)
        connState = res
        publish(res.online)
      } finally {
        inflight = false
      }
    }

    // An offline event is a useful hint, not a verdict. Android can fire it
    // during a brief radio handoff (mobile data → Wi‑Fi); publishing false
    // immediately makes the shell flash offline and can disable Send for a
    // moment even though the server is reachable again by the time the user
    // retries. Delay, then verify with the real /api/health probe. A matching
    // `online` event or visibility-triggered successful probe cancels this.
    const onOffline = () => {
      if (cancelled) return
      clearTimeout(offlineTimer)
      offlineTimer = setTimeout(() => { check() }, OFFLINE_EVENT_GRACE_MS)
    }
    const onOnline = () => {
      clearTimeout(offlineTimer)
      check()
    }
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
      clearTimeout(offlineTimer)
      clearInterval(interval)
      window.removeEventListener('online', onOnline)
      window.removeEventListener('offline', onOffline)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  return online
}
