const REQUEST_TIMEOUT_MS = 15_000

function cleanText(value, maximum = 256) {
  return typeof value === 'string' ? value.trim().slice(0, maximum) : ''
}

/** Host-mediated Projects access for opaque mini-app frames.
 *
 * The shell attributes every request to the exact AppCanvas window and only
 * exposes projects created from that app's own installed templates. Apps never
 * receive the owner's bearer token or arbitrary project filesystem access.
 */
export function makeProjects() {
  let sequence = 0
  const pending = new Map()

  function onMessage(event) {
    if (event.origin !== window.location.origin || event.source !== window.parent) return
    const message = event.data
    if (!message || message.type !== 'moebius:projects-result') return
    const request = pending.get(message.requestId)
    if (!request) return
    pending.delete(message.requestId)
    clearTimeout(request.timeout)
    if (message.ok === true) request.resolve(message.result)
    else request.reject(new Error(cleanText(message.error, 500) || 'Projects request failed.'))
  }

  window.addEventListener('message', onMessage)

  function request(action, payload = {}) {
    const requestId = `projects:${Date.now().toString(36)}:${(++sequence).toString(36)}`
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => {
        pending.delete(requestId)
        reject(new Error('Möbius did not answer the Projects request.'))
      }, REQUEST_TIMEOUT_MS)
      pending.set(requestId, { resolve, reject, timeout })
      window.parent.postMessage({
        type: 'moebius:projects',
        requestId,
        action,
        projectId: cleanText(payload.projectId, 128),
        templateId: cleanText(payload.templateId, 128),
        name: cleanText(payload.name),
      }, window.location.origin)
    })
  }

  return {
    list: () => request('list'),
    migrate: () => request('migrate'),
    create: ({ templateId, name } = {}) => request('create', { templateId, name }),
    open: projectId => request('open', { projectId }),
    browse: () => request('browse'),
    _destroy() {
      window.removeEventListener('message', onMessage)
      for (const value of pending.values()) {
        clearTimeout(value.timeout)
        value.reject(new Error('Projects runtime was replaced.'))
      }
      pending.clear()
    },
  }
}
