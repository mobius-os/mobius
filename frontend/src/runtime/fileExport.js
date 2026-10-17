// User-confirmed app-owned file export. The opaque frame never receives a
// shell credential or a filesystem capability; it only receives the outcome.
export function makeFileExport() {
  const pending = new Map()
  function onMessage(event) {
    if (event.source !== window.parent || event.origin !== window.location.origin) return
    const message = event.data
    if (message?.type !== 'moebius:file-export-result') return
    const settle = pending.get(message.requestId)
    if (!settle) return
    pending.delete(message.requestId)
    if (message.ok) settle.resolve(true)
    else if (message.cancelled) settle.resolve(false)
    else settle.reject(new Error(message.error || 'File export failed.'))
  }
  window.addEventListener('message', onMessage)
  return {
    save(path, name) {
      if (typeof path !== 'string' || !path) return Promise.reject(new Error('File path is required.'))
      const requestId = globalThis.crypto?.randomUUID?.()
        || `export-${Date.now()}-${Math.random().toString(36).slice(2)}`
      return new Promise((resolve, reject) => {
        pending.set(requestId, { resolve, reject })
        try {
          window.parent.postMessage({ type: 'moebius:file-export', requestId, path, name }, '*')
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
