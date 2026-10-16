/* One in-flight first-page detail read per chat, shared by drawer intent and activation. */

const inFlight = new Map()

/**
 * A drawer tap warms the chat's first compact page on pointerdown, and the
 * ChatView it opens activates a few frames later while that read is still in
 * flight. Both asked the server for the same transcript page, so every tap
 * decoded it twice. Share the network read, never a settled result: a read
 * that has finished is never reused, and each caller interprets the status
 * itself.
 *
 * `request(signal)` receives a signal owned by the shared read, not by any
 * caller. A caller's own `signal` stops only that caller waiting; once every
 * caller has left, the shared request is aborted so it does not hold a
 * connection open for a chat nobody is opening. A caller without a signal
 * stays until the read settles.
 */
export function readChatFirstPage(chatId, request, { signal } = {}) {
  if (signal?.aborted) return Promise.reject(signal.reason)
  const key = String(chatId)
  let entry = inFlight.get(key)
  if (!entry) {
    const controller = new AbortController()
    const created = { controller, waiters: 0, promise: null }
    created.promise = Promise.resolve()
      .then(() => request(controller.signal))
      .then(async response => ({
        status: response.status,
        ok: response.ok,
        data: response.ok ? await response.json() : null,
      }))
      .finally(() => {
        if (inFlight.get(key) === created) inFlight.delete(key)
      })
    inFlight.set(key, created)
    entry = created
  }
  return waitFor(key, entry, signal)
}

function waitFor(key, entry, signal) {
  entry.waiters += 1
  if (!signal) return entry.promise
  return new Promise((resolve, reject) => {
    let done = false
    const onAbort = () => {
      if (done) return
      done = true
      reject(signal.reason)
      entry.waiters -= 1
      if (entry.waiters > 0) return
      // The last waiter left: release the connection, and never let a later
      // caller join a read that is being cancelled.
      if (inFlight.get(key) === entry) inFlight.delete(key)
      entry.controller.abort(signal.reason)
    }
    signal.addEventListener('abort', onAbort, { once: true })
    entry.promise.then(resolve, reject).finally(() => {
      done = true
      signal.removeEventListener('abort', onAbort)
    })
  })
}

/**
 * A run signal makes an earlier read ineligible to join. A ChatView that
 * mounts after the signal treats it as already handled, so it must not adopt
 * a page read begun before it.
 */
export function forgetChatFirstPageRead(chatId) {
  inFlight.delete(String(chatId))
}

export function resetChatFirstPageReadsForTests() {
  inFlight.clear()
}
