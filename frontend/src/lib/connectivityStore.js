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
// Returning to a visible tab often beats the laptop's Wi-Fi/DNS wake-up. Keep
// the last confirmed verdict while the network settles, probe during that
// window, and only allow a failed probe to demote after the grace expires.
// This is deliberately presentation-free: consumers keep the existing boolean
// contract and the shell only shows its existing Offline status when the loss
// persists beyond a normal resume.
export const RESUME_NETWORK_GRACE_MS = 5000
export const RESUME_RETRY_MS = 1000
// A browser hint that disagrees with the probe is ambiguous in either
// direction. Confirm the first result quickly instead of leaving Send enabled
// or a recovered PWA labelled offline until the regular poll.
export const AMBIGUOUS_VERDICT_CONFIRM_MS = 1000

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
  let authoritativeReachabilityRevision = 0

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

  // An uncached mutation response is stronger evidence than navigator.onLine
  // or a cacheable health probe: it could only have come from the live server.
  // Let owning write paths repair a stale offline verdict immediately instead
  // of waiting for the next poll or Android's delayed `online` event.
  function reportReachable() {
    authoritativeReachabilityRevision += 1
    connectivityState = {
      successStreak: Math.max(1, connectivityState.successStreak),
      failureStreak: 0,
      online: true,
    }
    publish(true)
  }

  function startMonitor() {
    if (monitor) return monitor
    if (!windowTarget?.addEventListener || !documentTarget?.addEventListener) return null

    let cancelled = false
    let activeCheck = null
    let rerun = false
    let offlineTimer = null
    let confirmTimer = null
    let resumeGraceTimer = null
    let resumeRetryTimer = null
    let resumeGraceActive = false
    let interval = null

    function stopResumeRecovery() {
      resumeGraceActive = false
      if (resumeGraceTimer !== null) clearTimeoutFn(resumeGraceTimer)
      if (resumeRetryTimer !== null) clearTimeoutFn(resumeRetryTimer)
      resumeGraceTimer = null
      resumeRetryTimer = null
    }

    function scheduleResumeRetry() {
      if (!resumeGraceActive || resumeRetryTimer !== null) return
      resumeRetryTimer = setTimeoutFn(() => {
        resumeRetryTimer = null
        void check()
      }, RESUME_RETRY_MS)
    }

    function startResumeRecovery() {
      stopResumeRecovery()
      resumeGraceActive = true
      resumeGraceTimer = setTimeoutFn(() => {
        resumeGraceTimer = null
        resumeGraceActive = false
        if (resumeRetryTimer !== null) clearTimeoutFn(resumeRetryTimer)
        resumeRetryTimer = null
        void check()
      }, RESUME_NETWORK_GRACE_MS)
      void check()
    }

    function check() {
      if (activeCheck) {
        rerun = true
        return activeCheck
      }
      activeCheck = (async () => {
        const startedRevision = authoritativeReachabilityRevision
        const reachable = await probeReachable()
        if (cancelled) return reachable
        if (!reachable && startedRevision !== authoritativeReachabilityRevision) {
          // A mutation response arrived after this probe began. Its live-server
          // evidence is newer and stronger than the stale failed read.
          return true
        }
        if (!reachable && resumeGraceActive) {
          // A lid-open/tab-resume failure is not yet evidence of a lasting
          // outage. Preserve the last confirmed verdict and retry while the
          // laptop's network stack settles; the grace-ending check below is
          // allowed to publish Offline if the failure persists.
          scheduleResumeRetry()
          return false
        }
        const next = applyProbe(reachable)
        if (reachable && next.online && resumeGraceActive) stopResumeRecovery()
        if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
        confirmTimer = null
        // Either stale browser hint needs two matching probes. Run the second
        // promptly in BOTH directions: otherwise a stale-false navigator flag
        // leaves a genuinely reconnected PWA looking offline until the 20s
        // interval, the exact resume-from-background failure this streak is
        // meant to prevent.
        const needsFailureConfirmation = (
          !reachable && next.online && next.failureStreak === 1
        )
        const needsRecoveryConfirmation = (
          reachable && !next.online && next.successStreak === 1
        )
        if (needsFailureConfirmation || needsRecoveryConfirmation) {
          confirmTimer = setTimeoutFn(
            () => { void check() },
            AMBIGUOUS_VERDICT_CONFIRM_MS,
          )
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
    const onForeground = () => {
      if (documentTarget.visibilityState !== 'visible') {
        stopResumeRecovery()
        return
      }
      if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
      if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
      offlineTimer = null
      confirmTimer = null
      startResumeRecovery()
    }

    const current = {
      check,
      stop() {
        if (cancelled) return
        cancelled = true
        if (offlineTimer !== null) clearTimeoutFn(offlineTimer)
        if (confirmTimer !== null) clearTimeoutFn(confirmTimer)
        stopResumeRecovery()
        if (interval !== null) clearIntervalFn(interval)
        windowTarget.removeEventListener('online', onOnline)
        windowTarget.removeEventListener('offline', onOffline)
        windowTarget.removeEventListener('focus', onForeground)
        windowTarget.removeEventListener('pageshow', onForeground)
        documentTarget.removeEventListener('visibilitychange', onForeground)
        if (monitor === current) monitor = null
      },
    }
    monitor = current
    windowTarget.addEventListener('online', onOnline)
    windowTarget.addEventListener('offline', onOffline)
    // A laptop can sleep and wake without changing document.visibilityState.
    // Window focus covers reopening Chrome in that exact retained-tab case;
    // pageshow covers a page restored from the back/forward cache.
    windowTarget.addEventListener('focus', onForeground)
    windowTarget.addEventListener('pageshow', onForeground)
    documentTarget.addEventListener('visibilitychange', onForeground)
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
    const startedRevision = authoritativeReachabilityRevision
    verificationCheck = probeReachable()
      .then((reachable) => {
        if (
          !reachable
          && startedRevision !== authoritativeReachabilityRevision
        ) return true
        applyProbe(reachable)
        return reachable
      })
      .finally(() => { verificationCheck = null })
    return verificationCheck
  }

  return { getSnapshot, subscribe, verify, reportReachable }
}

const connectivityStore = createConnectivityStore()

export const getOnlineSnapshot = connectivityStore.getSnapshot
export const subscribeOnline = connectivityStore.subscribe
export const verifyConnectivity = connectivityStore.verify
export const reportNetworkReachable = connectivityStore.reportReachable
