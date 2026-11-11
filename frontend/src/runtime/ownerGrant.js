// Owner confirmation of one sensitive action. The shell shows the owner a
// sheet outside this frame; only the owner's tap records a short-lived, single-use
// grant on the server. The frame never receives the grant: it repeats its normal request.
//
// A shell tab that predates this request never answers it, and the promise would
// never settle. The shell acks a request it accepts, so only the wait for that
// ack is short; once acked, the owner may take as long as they like.
const ACK_TIMEOUT_MS = 3000
const UNANSWERED = 'Möbius did not answer. Reload Möbius, then try again.'

export function makeOwnerGrant() {
  const pending = new Map()
  function settleWith(requestId, finish) {
    const settle = pending.get(requestId)
    if (!settle) return
    pending.delete(requestId)
    clearTimeout(settle.timer)
    finish(settle)
  }
  function onMessage(event) {
    if (event.source !== window.parent || event.origin !== window.location.origin) return
    const message = event.data
    if (message?.type === 'moebius:owner-grant-ack') {
      clearTimeout(pending.get(message.requestId)?.timer)
      return
    }
    if (message?.type !== 'moebius:owner-grant-result') return
    settleWith(message.requestId, (settle) => {
      if (message.ok) settle.resolve(true)
      else if (message.cancelled) settle.resolve(false)
      else settle.reject(Object.assign(new Error(message.error || 'Confirmation failed.'),
        message.code ? { code: message.code } : {}))
    })
  }
  window.addEventListener('message', onMessage)
  return {
    request(action, scope = {}, body = {}) {
      if (typeof action !== 'string' || !action) return Promise.reject(new Error('Action is required.'))
      const requestId = globalThis.crypto?.randomUUID?.()
        || `owner-grant-${Date.now()}-${Math.random().toString(36).slice(2)}`
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => settleWith(requestId, () => reject(
          Object.assign(new Error(UNANSWERED), { code: 'owner_grant_unanswered' }))), ACK_TIMEOUT_MS)
        pending.set(requestId, { resolve, reject, timer })
        try {
          window.parent.postMessage({ type: 'moebius:owner-grant', requestId, action, scope, body }, '*')
        } catch (error) {
          settleWith(requestId, () => reject(error))
        }
      })
    },
    _destroy() {
      window.removeEventListener('message', onMessage)
      for (const settle of pending.values()) {
        clearTimeout(settle.timer)
        settle.resolve(false)
      }
      pending.clear()
    },
  }
}
