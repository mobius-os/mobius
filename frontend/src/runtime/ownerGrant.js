// Owner confirmation of one sensitive action. The shell shows the owner a
// sheet outside this frame; only the owner's tap records a short-lived grant on
// the server. The frame never receives the grant: it repeats its normal request.
export function makeOwnerGrant() {
  const pending = new Map()
  function onMessage(event) {
    if (event.source !== window.parent || event.origin !== window.location.origin) return
    const message = event.data
    if (message?.type !== 'moebius:owner-grant-result') return
    const settle = pending.get(message.requestId)
    if (!settle) return
    pending.delete(message.requestId)
    if (message.ok) settle.resolve(true)
    else if (message.cancelled) settle.resolve(false)
    else settle.reject(Object.assign(new Error(message.error || 'Confirmation failed.'),
      message.code ? { code: message.code } : {}))
  }
  window.addEventListener('message', onMessage)
  return {
    request(action, scope = {}, body = {}) {
      if (typeof action !== 'string' || !action) return Promise.reject(new Error('Action is required.'))
      const requestId = globalThis.crypto?.randomUUID?.()
        || `owner-grant-${Date.now()}-${Math.random().toString(36).slice(2)}`
      return new Promise((resolve, reject) => {
        pending.set(requestId, { resolve, reject })
        try {
          window.parent.postMessage({ type: 'moebius:owner-grant', requestId, action, scope, body }, '*')
        } catch (error) {
          pending.delete(requestId)
          reject(error)
        }
      })
    },
    _destroy() {
      window.removeEventListener('message', onMessage)
      for (const settle of pending.values()) settle.resolve(false)
      pending.clear()
    },
  }
}
