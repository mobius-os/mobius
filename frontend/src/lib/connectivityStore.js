// One owner for transport reachability and chat delivery readiness.
// Any HTTP response proves reachability, but only /api/ready permits delivery.
// Browsing remains usable during uncertainty; durable sends wait locally.
const READINESS_URL = '/api/ready'

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

  if (
    evidence.type === 'checking'
    && state.phase === ReachabilityPhase.ONLINE
  ) {
    return {
      ...state,
      phase: ReachabilityPhase.CHECKING,
      staleOfflineSuccesses: 0,
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
  fetchImpl = (...args) => globalThis.fetch(...args),
  AbortControllerImpl = typeof AbortController === 'undefined' ? null : AbortController,
  setTimeoutFn = setTimeout,
  clearTimeoutFn = clearTimeout,
} = {}) {
  const listeners = new Set()
  let state = initialReachabilityState(navigatorTarget?.onLine !== false)
  let monitor = null
  let standaloneCheck = null
  let evidenceRevision = 0
  let readinessRevision = 0
  let ready = false
  let restartPending = false
  let restartSourceBootId = null
  let restartFromLegacyServer = false

  function getSnapshot() { return publicOnline(state) }
  function getPhaseSnapshot() { return state.phase }
  function getRecoverySnapshot() { return state.recoveryGeneration }
  function getState() { return state }
  function getDeliveryReadySnapshot() {
    return state.phase === ReachabilityPhase.ONLINE && ready && !restartPending
  }
  function getRestartPendingSnapshot() { return restartPending }
  function notify() { listeners.forEach(listener => listener()) }
  function setReady(value) {
    if (ready === value) return
    ready = value
    notify()
  }
  function setRestartPending(sourceBootId) {
    evidenceRevision += 1
    readinessRevision += 1
    restartSourceBootId = typeof sourceBootId === 'string' && sourceBootId.trim()
      ? sourceBootId : null
    // Old servers omit the field entirely. If this event beats our first
    // probe, a later boot-identifying readiness response still proves that
    // the upgraded backend replaced that legacy event producer.
    restartFromLegacyServer = sourceBootId === undefined
    const changed = !restartPending
    restartPending = true
    ready = false
    if (changed) notify()
    void verify()
  }

  function publish(next) {
    // Recovery consumers may deliver work: transport alone is not that edge.
    next = { ...next, recoveryGeneration: state.recoveryGeneration }
    if (sameState(state, next)) return false
    const previousPhase = state.phase
    const previousRecovery = state.recoveryGeneration
    state = next
    if (next.phase !== ReachabilityPhase.ONLINE) ready = false
    if (
      previousPhase !== next.phase
      || previousRecovery !== next.recoveryGeneration
    ) listeners.forEach(listener => listener())
    return true
  }

  async function probeReadiness() {
    const startedReadinessRevision = readinessRevision
    if (typeof fetchImpl !== 'function') return { reachable: false }
    let timer = null
    const controller = AbortControllerImpl ? new AbortControllerImpl() : null
    try {
      if (controller) timer = setTimeoutFn(() => controller.abort(), PROBE_TIMEOUT_MS)
      const response = await fetchImpl(READINESS_URL, {
        method: 'GET', cache: 'no-store', signal: controller?.signal,
      })
      let body
      try { body = await response.json() } catch { /* A proxy response is not readiness. */ }
      const isReadinessBody = typeof body?.ready === 'boolean'
      const readinessBootId = isReadinessBody && typeof body?.boot_id === 'string' && body.boot_id.trim()
        ? body.boot_id : null
      const legacyReadiness = isReadinessBody && body.boot_id === undefined
      return {
        reachable: true,
        startedReadinessRevision,
        ready: response.ok === true && body?.ready === true
          && (legacyReadiness || readinessBootId !== null),
        bootId: readinessBootId,
      }
    } catch {
      return { reachable: false }
    } finally {
      if (timer !== null) clearTimeoutFn(timer)
    }
  }

  function reportReachable() {
    const wasUncertain = state.phase !== ReachabilityPhase.ONLINE
    evidenceRevision += 1
    publish(reduceReachability(state, {
      type: 'reachable', strong: true, navigatorOnline: true,
    }))
    if (getDeliveryReadySnapshot()) monitor?.settleRecovery()
    else if (wasUncertain) monitor?.check()
  }

  function applyEvidence(result, startedRevision) {
    // A response started before an observed restart/failure cannot clear it.
    if (!result.reachable && startedRevision !== evidenceRevision) return true
    if (result.reachable && result.startedReadinessRevision !== readinessRevision) return true
    if (result.reachable) {
      const wasReady = getDeliveryReadySnapshot()
      publish(reduceReachability(state, {
        type: 'reachable', strong: false,
        navigatorOnline: navigatorTarget?.onLine !== false,
      }))
      // Readiness and boot identity must come from the same response. The
      // legacy producer omitted boot identity from both events and readiness;
      // a modern readiness response therefore also proves its replacement.
      const laterBoot = result.bootId && restartSourceBootId
        && result.bootId !== restartSourceBootId
      const upgradedLegacyServer = restartFromLegacyServer && result.bootId
      if (restartPending && result.ready && (laterBoot || upgradedLegacyServer)) {
        restartPending = false
        restartSourceBootId = null
        restartFromLegacyServer = false
        notify()
      }
      setReady(result.ready === true)
      if (!wasReady && getDeliveryReadySnapshot()) {
        state = { ...state, recoveryGeneration: state.recoveryGeneration + 1 }
        notify()
      }
    } else {
      publish(reduceReachability(state, { type: 'failed' }))
    }
    return result.reachable
  }

  function startMonitor() {
    if (monitor) return monitor
    if (!windowTarget?.addEventListener || !documentTarget?.addEventListener) return null

    let cancelled = false
    let activeCheck = null
    let checkGeneration = 0
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
    function applyProbe(result, startedRevision) {
      const reachable = applyEvidence(result, startedRevision)
      if (getDeliveryReadySnapshot()) settleRecovery()
      else {
        if (!reachable) beginFailureWindow()
        // Reachable-but-not-ready is a service state, never device Offline.
        else {
          clearFailureDeadline()
          if (state.phase === ReachabilityPhase.OFFLINE) {
            clearRetry()
            retryAttempt = 0
          }
        }
        scheduleRetry()
      }
      return reachable
    }
    // A browser may suspend an in-flight fetch while a tab is backgrounded.
    // Detach that attempt at the lifecycle boundary so foreground recovery is
    // never serialized behind a promise the browser may no longer settle.
    // The generation guard also prevents a late failure from overwriting the
    // fresh foreground verdict.
    function abandonActiveCheck() {
      checkGeneration += 1
      activeCheck = null
      rerun = false
    }
    function check({ fresh = false } = {}) {
      if (fresh && activeCheck) abandonActiveCheck()
      if (activeCheck) {
        rerun = true
        return activeCheck
      }
      const startedRevision = evidenceRevision
      const generation = ++checkGeneration
      const current = probeReadiness()
        .then(reachable => cancelled || generation !== checkGeneration
          ? reachable
          : applyProbe(reachable, startedRevision))
        .finally(() => {
          if (activeCheck !== current) return
          activeCheck = null
          if (rerun && !cancelled) {
            rerun = false
            void check()
          }
        })
      activeCheck = current
      return current
    }
    function requestCheck() {
      if (!visible()) return
      evidenceRevision += 1
      readinessRevision += 1
      publish(reduceReachability(state, { type: 'checking' }))
      void check()
    }
    function onVisibilityChange() {
      if (!visible()) {
        // Background suspension is not evidence that the server went away.
        // Retire both the pending verdict and its deadline; foreground return
        // starts one independent probe instead of inheriting stale work.
        clearFailureDeadline()
        clearRetry()
        readinessRevision += 1
        setReady(false)
        abandonActiveCheck()
        return
      }
      void check({ fresh: true })
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
    // Callers invoke verification after transport evidence such as a failed
    // request or an unexpected stream close. That evidence belongs to one
    // transport, not necessarily to the server: keep the last reachable verdict
    // while the bounded health probe decides. Publishing Checking before the
    // probe made every healthy stream reconnect flash the shell status dot.
    // A failed probe still enters Checking through applyProbe(), starts the
    // failure grace window, and eventually confirms Offline.
    // A hidden tab is the exception: browsers intentionally suspend its
    // transports and timers, so defer to the monitor's foreground boundary
    // instead of publishing a false Checking state that can become stranded.
    if (documentTarget?.visibilityState === 'hidden') {
      return Promise.resolve(publicOnline(state))
    }

    if (monitor) return monitor.check()
    if (standaloneCheck) return standaloneCheck
    const startedRevision = evidenceRevision
    standaloneCheck = probeReadiness()
      .then(result => applyEvidence(result, startedRevision))
      .finally(() => { standaloneCheck = null })
    return standaloneCheck
  }

  return {
    getSnapshot,
    getPhaseSnapshot,
    getRecoverySnapshot,
    getState,
    getDeliveryReadySnapshot,
    getRestartPendingSnapshot,
    setRestartPending,
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

export const getDeliveryReadySnapshot = connectivityStore.getDeliveryReadySnapshot
export const subscribeDeliveryReady = connectivityStore.subscribe
export const getRestartPendingSnapshot = connectivityStore.getRestartPendingSnapshot
export const subscribeRestart = connectivityStore.subscribe
export const setRestartPending = connectivityStore.setRestartPending
