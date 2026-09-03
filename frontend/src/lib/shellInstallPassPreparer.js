/**
 * Build one process-wide preparer for the shell's iOS installation handoff.
 *
 * A normal browser tab prepares once. A real lifecycle return or an explicit
 * install action may force a fresh one because an installed copy could have
 * consumed the previous grant while Safari was away. Concurrent signals share
 * one request; failure remains retryable and never blocks installation.
 */
export function createShellInstallPassPreparer({
  request,
  isIos,
  isStandalone,
  hasToken,
}) {
  let prepared = false
  let pending = null
  let activeController = null
  let stopped = false

  async function prepare({ force = false, signal } = {}) {
    if (stopped) return false
    if (!isIos() || isStandalone() || !hasToken()) return false
    if (prepared && !force) return true
    if (pending) return pending

    activeController = new AbortController()
    const requestController = activeController
    let removeCallerAbort
    if (signal) {
      const relayCallerAbort = () => requestController.abort(signal.reason)
      if (signal.aborted) relayCallerAbort()
      else {
        signal.addEventListener('abort', relayCallerAbort, { once: true })
        removeCallerAbort = () => {
          signal.removeEventListener('abort', relayCallerAbort)
        }
      }
    }
    pending = Promise.resolve()
      .then(() => request({ signal: requestController.signal }))
      .then((response) => {
        prepared = Boolean(response?.ok)
        return prepared
      })
      .catch(() => {
        prepared = false
        return false
      })
      .finally(() => {
        removeCallerAbort?.()
        pending = null
        activeController = null
      })
    return pending
  }

  async function stop() {
    stopped = true
    prepared = false
    const inFlight = pending
    activeController?.abort()
    if (inFlight) await inFlight
  }

  return { prepare, stop }
}
