// One process-wide owner for server reachability.
//
// Browser online/offline events are prompts, not verdicts. Any HTTP response
// proves that the server is reachable; only a network failure can move the
// state toward Offline. The public boolean deliberately maps Checking to
// online so a cold radio or laptop wake does not disable the product.
const HEALTH_URL = '/api/health'

export const PROBE_TIMEOUT_MS = 3000
export const FAILURE_GRACE_MS = 5000
export const RECOVERY_RETRY_MIN_MS = 1000
export const RECOVERY_RETRY_MAX_MS = 30000
export const STALE_OFFLINE_SUCCESS_THRESHOLD = 2

export const ReachabilityPhase = Object.freeze({
  ONLINE: 'online',
  CHECKING: 'checking',
  OFFLINE: 'offline',
})

export function publicOnline(state) {
  return state.phase !== ReachabilityPhase.OFFLINE
}

export function initialReachabilityState(navigatorOnline = true) {
  return {
    phase: navigatorOnline === false
      ? ReachabilityPhase.OFFLINE
      : ReachabilityPhase.ONLINE,
    staleOfflineSuccesses: 0,
    recoveryGeneration: 0,
  }
}

/** Pure evidence reducer. Timers only decide when to supply `deadline`. */
export function reduceReachability(state, evidence) {
  if (evidence.type === 'reachable') {
    const needsStaleFlagConfirmation = (
      state.phase === ReachabilityPhase.OFFLINE
      && evidence.strong !== true
      && evidence.navigatorOnline === false
    )
    if (needsStaleFlagConfirmation) {
      const streak = state.staleOfflineSuccesses + 1
      if (streak < STALE_OFFLINE_SUCCESS_THRESHOLD) {
        return { ...state, staleOfflineSuccesses: streak }
      }
    }
    const recovered = state.phase !== ReachabilityPhase.ONLINE
      || state.staleOfflineSuccesses > 0
    return {
      phase: ReachabilityPhase.ONLINE,
      staleOfflineSuccesses: 0,
      recoveryGeneration: state.recoveryGeneration + (recovered ? 1 : 0),
    }
  }

  if (evidence.type === 'failed') {
    if (state.phase === ReachabilityPhase.OFFLINE) {
      return { ...state, staleOfflineSuccesses: 0 }
    }
    return {
      ...state,
      phase: ReachabilityPhase.CHECKING,
      staleOfflineSuccesses: 0,
    }
  }

  if (evidence.type === 'deadline' && state.phase === ReachabilityPhase.CHECKING) {
    return { ...state, phase: ReachabilityPhase.OFFLINE, staleOfflineSuccesses: 0 }
  }

  return state
}

function sameState(a, b) {
  return a.phase === b.phase
    && a.staleOfflineSuccesses === b.staleOfflineSuccesses
    && a.recoveryGeneration === b.recoveryGeneration
}

export function createConnectivityStore({
  windowTarget = typeof window === 'undefined' ? null : window,
  documentTarget = typeof document === 'undefined' ? null : document,
  navigatorTarget = typeof navigator === 'undefined' ? null : navigator,
  fetchImpl = typeof fetch === 'undefined' ? null : fetch,
  AbortControllerImpl = typeof AbortController === 'undefined' ? null : AbortController,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  const listeners = new Set()
  let state = initialReachabilityState(navigatorTarget?.onLine !== false)
  let monitor = null
  let standaloneCheck = null
  let evidenceRevision = 0

  function getSnapshot() { return publicOnline(state) }
  function getPhaseSnapshot() { return state.phase }
  function getRecoverySnapshot() { return state.recoveryGeneration }
  function getState() { return state }

  function publish(next) {
    if (sameState(state, next)) return false
    const previousPhase = state.phase
    const previousRecovery = state.recoveryGeneration
    state = next
    if (
      previousPhase !== next.phase
      || previousRecovery !== next.recoveryGeneration
    ) listeners.forEach(listener => listener())
    return true
  }

  async function probeReachable() {
    if (typeof fetchImpl !== 'function') return false
    let timer = null
    const controller = AbortControllerImpl ? new AbortControllerImpl() : null
    try {
      if (controller) timer = setTimeoutFn(() => controller.abort(), PROBE_TIMEOUT_MS)
      // Any response is transport success. Authentication and server errors
      // belong to the request owner and must never masquerade as Offline.
      await fetchImpl(HEALTH_URL, {
        method: 'GET', cache: 'no-store', signal: controller?.signal,
      })
      return true
    } catch {
      return false
    } finally {
      if (timer !== null) clearTimeoutFn(timer)
    }
  }

  function reportReachable() {
    evidenceRevision += 1
    publish(reduceReachability(state, {
      type: 'reachable', strong: true, navigatorOnline: true,
    }))
    monitor?.settleRecovery()
  }

  function startMonitor() {
    if (monitor) return monitor
    if (!windowTarget?.addEventListener || !documentTarget?.addEventListener) return null

    let cancelled = false
    let activeCheck = null
    let rerun = false
    let failureTimer = null
    let retryTimer = null
    let retryAttempt = 0

    function visible() { return documentTarget.visibilityState !== 'hidden' }
    function clearFailureDeadline() {
      if (failureTimer !== null) clearTimeoutFn(failureTimer)
      failureTimer = null
    }
    function clearRetry() {
      if (retryTimer !== null) clearTimeoutFn(retryTimer)
      retryTimer = null
    }
    function settleRecovery() {
      clearFailureDeadline()
      clearRetry()
      retryAttempt = 0
    }
    function retryDelay() {
      return Math.min(
        RECOVERY_RETRY_MIN_MS * (2 ** retryAttempt),
        RECOVERY_RETRY_MAX_MS,
      )
    }
    function scheduleRetry() {
      if (cancelled || !visible() || retryTimer !== null) return
      const delay = retryDelay()
      retryAttempt += 1
      retryTimer = setTimeoutFn(() => {
        retryTimer = null
        void check()
      }, delay)
    }
    function beginFailureWindow() {
      if (failureTimer !== null || state.phase !== ReachabilityPhase.CHECKING) return
      failureTimer = setTimeoutFn(() => {
        failureTimer = null
        publish(reduceReachability(state, { type: 'deadline' }))
        scheduleRetry()
      }, FAILURE_GRACE_MS)
    }
    function applyProbe(reachable, startedRevision) {
      if (!reachable && startedRevision !== evidenceRevision) return true
      if (reachable) {
        const next = reduceReachability(state, {
          type: 'reachable',
          strong: false,
          navigatorOnline: navigatorTarget?.onLine !== false,
        })
        publish(next)
        if (next.phase === ReachabilityPhase.ONLINE) settleRecovery()
        else {
          // One real response while navigator.onLine is stale-false is useful
          // progress. Confirm it promptly rather than inheriting an outage's
          // potentially long exponential backoff.
          clearRetry()
          retryAttempt = 0
          scheduleRetry()
        }
        return true
      }
      publish(reduceReachability(state, { type: 'failed' }))
      beginFailureWindow()
      scheduleRetry()
      return false
    }
    function check() {
      if (activeCheck) {
        rerun = true
        return activeCheck
      }
      const startedRevision = evidenceRevision
      activeCheck = probeReachable()
        .then(reachable => cancelled
          ? reachable
          : applyProbe(reachable, startedRevision))
        .finally(() => {
          activeCheck = null
          if (rerun && !cancelled) {
            rerun = false
            void check()
          }
        })
      return activeCheck
    }
    function requestCheck() {
      if (!visible()) return
      void check()
    }
    function onVisibilityChange() {
      if (!visible()) {
        clearRetry()
        return
      }
      requestCheck()
    }

    const current = {
      check,
      settleRecovery,
      stop() {
        if (cancelled) return
        cancelled = true
        clearFailureDeadline()
        clearRetry()
        windowTarget.removeEventListener('online', requestCheck)
        windowTarget.removeEventListener('offline', requestCheck)
        windowTarget.removeEventListener('focus', requestCheck)
        windowTarget.removeEventListener('pageshow', requestCheck)
        documentTarget.removeEventListener('visibilitychange', onVisibilityChange)
        if (monitor === current) monitor = null
      },
    }
    monitor = current
    windowTarget.addEventListener('online', requestCheck)
    windowTarget.addEventListener('offline', requestCheck)
    windowTarget.addEventListener('focus', requestCheck)
    windowTarget.addEventListener('pageshow', requestCheck)
    documentTarget.addEventListener('visibilitychange', onVisibilityChange)
    requestCheck()
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

  function verify() {
    if (monitor) return monitor.check()
    if (standaloneCheck) return standaloneCheck
    const startedRevision = evidenceRevision
    standaloneCheck = probeReachable()
      .then(reachable => {
        if (!reachable && startedRevision !== evidenceRevision) return true
        publish(reduceReachability(state, reachable
          ? {
              type: 'reachable', strong: false,
              navigatorOnline: navigatorTarget?.onLine !== false,
            }
          : { type: 'failed' }))
        return reachable
      })
      .finally(() => { standaloneCheck = null })
    return standaloneCheck
  }

  return {
    getSnapshot,
    getPhaseSnapshot,
    getRecoverySnapshot,
    getState,
    subscribe,
    verify,
    reportReachable,
  }
}

const connectivityStore = createConnectivityStore()

export const getOnlineSnapshot = connectivityStore.getSnapshot
export const getReachabilityPhaseSnapshot = connectivityStore.getPhaseSnapshot
export const getRecoverySnapshot = connectivityStore.getRecoverySnapshot
export const subscribeOnline = connectivityStore.subscribe
export const subscribeRecovery = connectivityStore.subscribe
export const verifyConnectivity = connectivityStore.verify
export const reportNetworkReachable = connectivityStore.reportReachable
