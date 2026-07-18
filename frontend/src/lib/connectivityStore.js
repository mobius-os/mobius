import { resolveOnline } from './onlineStatus.js'

// navigator.onLine is only a hint in the service-worker PWA: cached requests
// can leave it stale in either direction. A no-store /api/health request is the
// reachability verdict, while resolveOnline supplies the asymmetric hysteresis
// that rejects one cold-radio failure and one stale-radio success. We always
// probe—even when the flag says offline—so reconnect recovery cannot wedge.
//
// The monitor is process-wide because the shell retains multiple chats and an
// app canvas at once. Giving each hook instance its own browser listeners and
// 20-second poll made panes disagree and multiplied mobile radio wakeups. The
// first subscriber starts this store; the last one tears every resource down.
const HEALTH_URL = '/api/health'
// A healthy server answers quickly, but a mobile radio waking from background
// can need longer. The cap mainly bounds Android fetches that remain pending
// after the network disappears; AppCanvas also waits on this verdict before it
// chooses its offline-safe credential path.
export const PROBE_TIMEOUT_MS = 3000
// Treat an OS offline event as a prompt to verify after a handoff grace, never
// as truth. Android can emit it transiently while moving between radios.
export const OFFLINE_EVENT_GRACE_MS = 2500
export const POLL_INTERVAL_MS = 20000
// navigator=true + probe=false is ambiguous and needs two failures. Confirm the
// first quickly instead of leaving chat Send enabled until the regular poll.
export const AMBIGUOUS_FAILURE_CONFIRM_MS = 1000

/**
 * One reachability monitor shared by every shell consumer. The dependency
 * arguments keep the state machine directly testable without browser globals.
 */
export function createConnectivityStore({
  windowTarget = typeof window === 'undefined' ? null : window,
  documentTarget = typeof document === 'undefined' ? null : document,
  navigatorTarget = typeof navigator === 'undefined' ? null : navigator,
  fetchImpl = typeof fetch === 'undefined' ? null : fetch,
  AbortControllerImpl = typeof AbortController === 'undefined' ? null : AbortController,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
  setIntervalFn = setInterval,
  clearIntervalFn = clearInterval,
} = {}) {
  const listeners = new Set()
  let snapshot = navigatorTarget?.onLine !== false
  let connectivityState = { successStreak: 0, failureStreak: 0, online: snapshot }
  let monitor = null
  let verificationCheck = null

  function getSnapshot() {
    return snapshot
  }

  function publish(next) {
    if (snapshot === next) return
    snapshot = next
    listeners.forEach((listener) => listener())
  }

  async function probeReachable() {
    if (typeof fetchImpl !== 'function') return false
    let timer = null
    const controller = AbortControllerImpl ? new AbortControllerImpl() : null
    try {
      if (controller) {
        timer = setTimeoutFn(() => controller.abort(), PROBE_TIMEOUT_MS)
      }
      const response = await fetchImpl(HEALTH_URL, {
        method: 'GET',
        cache: 'no-store',
        signal: controller?.signal,
      })
      return response.ok
    } catch {
      return false
    } finally {
      if (timer !== null) clearTimeoutFn(timer)
    }
  }

  function applyProbe(reachable) {
    connectivityState = resolveOnline(
      reachable,
      navigatorTarget?.onLine !== false,
      connectivityState,
    )
    publish(connectivityState.online)
    return connectivityState
  }

  function startMonitor() {
    if (monitor) return monitor
    if (!windowTarget?.addEventListener || !documentTarget?.addEventListener) return null

    let cancelled = false
    let activeCheck = null
    let rerun = false
    let offlineTimer = null
    let confirmTimer = null
    let interval = null

    function check() {
      if (activeCheck) {
        rerun = true
        return activeCheck
      }
      activeCheck = (async () => {
        const reachable = await probeReachable()
        if (cancelled) return reachable
        const next = applyProbe(reachable)
        if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
        confirmTimer = null
        // A stale-true navigator flag needs two failures. Run the confirming
        // probe promptly instead of leaving Send enabled until the 20s poll.
        if (!reachable && next.online && next.failureStreak === 1) {
          confirmTimer = setTimeoutFn(() => { void check() }, AMBIGUOUS_FAILURE_CONFIRM_MS)
        }
        return reachable
      })().finally(() => {
        activeCheck = null
        if (rerun && !cancelled) {
          rerun = false
          void check()
        }
      })
      return activeCheck
    }

    const onOffline = () => {
      if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
      if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
      confirmTimer = null
      offlineTimer = setTimeoutFn(() => { void check() }, OFFLINE_EVENT_GRACE_MS)
    }
    const onOnline = () => {
      if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
      if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
      offlineTimer = null
      confirmTimer = null
      void check()
    }
    const onVisible = () => {
      if (documentTarget.visibilityState !== 'visible') return
      if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
      offlineTimer = null
      void check()
    }

    const current = {
      check,
      stop() {
        if (cancelled) return
        cancelled = true
        if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
        if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
        if (interval !== null) clearIntervalFn(interval)
        windowTarget.removeEventListener('online', onOnline)
        windowTarget.removeEventListener('offline', onOffline)
        documentTarget.removeEventListener('visibilitychange', onVisible)
        if (monitor === current) monitor = null
      },
    }
    monitor = current
    windowTarget.addEventListener('online', onOnline)
    windowTarget.addEventListener('offline', onOffline)
    documentTarget.addEventListener('visibilitychange', onVisible)
    interval = setIntervalFn(() => {
      if (documentTarget.visibilityState === 'visible') void check()
    }, POLL_INTERVAL_MS)
    void check()
    return current
  }

  function subscribe(listener) {
    listeners.add(listener)
    startMonitor()
    let subscribed = true
    return () => {
      if (!subscribed) return
      subscribed = false
      listeners.delete(listener)
      if (listeners.size === 0) monitor?.stop()
    }
  }

  // A failed API request can request a fresh verdict. With mounted consumers,
  // reuse their coalesced monitor. Without consumers, perform one bounded probe
  // only—never create an ownerless polling interval.
  function verify() {
    if (monitor) return monitor.check()
    if (verificationCheck) return verificationCheck
    verificationCheck = probeReachable()
      .then((reachable) => {
        applyProbe(reachable)
        return reachable
      })
      .finally(() => { verificationCheck = null })
    return verificationCheck
  }

  return { getSnapshot, subscribe, verify }
}

const connectivityStore = createConnectivityStore()

export const getOnlineSnapshot = connectivityStore.getSnapshot
export const subscribeOnline = connectivityStore.subscribe
export const verifyConnectivity = connectivityStore.verify
