/**
 * Owns one screen wake lock for a user-visible activity.
 *
 * Browsers release screen wake locks when the document is hidden. Keep the
 * activity intent separate from the current sentinel so a still-active voice
 * session can reacquire the lock when the app becomes visible again.
 */
export function createScreenWakeLockController({
  manager = globalThis.navigator?.wakeLock,
  documentTarget = globalThis.document,
} = {}) {
  let active = false
  let sentinel = null
  let requestPending = false
  let reacquireAfterPending = false
  let requestVersion = 0

  function releaseSentinel() {
    const held = sentinel
    sentinel = null
    if (!held) return
    try { held.release()?.catch?.(() => {}) } catch { /* best-effort release */ }
  }

  async function acquire() {
    if (
      !active
      || requestPending
      || sentinel
      || typeof manager?.request !== 'function'
      || documentTarget?.visibilityState !== 'visible'
    ) return

    requestPending = true
    const version = requestVersion
    try {
      const acquired = await manager.request('screen')
      // A user can stop dictation while request() is still awaiting browser
      // permission/platform acquisition. Never let that late result leak a
      // lock beyond the activity that requested it.
      if (
        !active
        || version !== requestVersion
        || documentTarget?.visibilityState !== 'visible'
      ) {
        try { await acquired.release() } catch { /* best-effort release */ }
        return
      }

      sentinel = acquired
      acquired.addEventListener?.('release', () => {
        if (sentinel === acquired) sentinel = null
      }, { once: true })
    } catch {
      // Unsupported platform policy, low-power mode, or a denied permission
      // should not break voice input. The wake lock is an enhancement.
    } finally {
      requestPending = false
      if (reacquireAfterPending) {
        reacquireAfterPending = false
        void acquire()
      }
    }
  }

  function onVisibilityChange() {
    if (documentTarget?.visibilityState === 'visible') {
      if (requestPending) reacquireAfterPending = true
      else void acquire()
    } else {
      // User agents release this automatically; doing it explicitly also
      // makes the ownership boundary deterministic in tests and older builds.
      releaseSentinel()
    }
  }

  function start() {
    if (active) return
    active = true
    requestVersion += 1
    documentTarget?.addEventListener?.('visibilitychange', onVisibilityChange)
    if (requestPending) reacquireAfterPending = true
    else void acquire()
  }

  function stop() {
    if (!active) return
    active = false
    requestVersion += 1
    reacquireAfterPending = false
    documentTarget?.removeEventListener?.('visibilitychange', onVisibilityChange)
    releaseSentinel()
  }

  return { start, stop }
}
