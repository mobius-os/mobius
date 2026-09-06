/**
 * The anonymous public app host: the parent page behind `/<slug>`.
 *
 * It carries only a short-lived exact-app public token, never an owner
 * session. It fetches the app module, answers the runtime's storage RPC
 * against the public storage endpoints, and serves exactly one reviewed
 * browser capability (device.storage) through the same capability host and
 * provider the signed-in shell uses. The server hands it its configuration in
 * the `mobius-public-host` JSON slot.
 */
import { createCapabilityHost } from '../lib/capabilityHost.js'
import { createDeviceStorageProvider, DEVICE_STORAGE } from '../lib/deviceStorage.js'

const config = JSON.parse(document.getElementById('mobius-public-host').textContent)
const APP_ID = config.appId
const TOKEN = config.token
const VERSION = config.version
const APP_INSTANCE = config.appInstance
const CAPABILITY_CONTRACT = config.capabilityContract || {}
const FRAME_URL = `/api/apps/${APP_ID}/frame?v=${encodeURIComponent(VERSION)}`
const BACKGROUND = '#101514'

const frame = document.getElementById('app')
const status = document.getElementById('status')
const authHeaders = { Authorization: `Bearer ${TOKEN}` }

function send(message, transfer = []) {
  try { frame.contentWindow.postMessage(message, '*', transfer) } catch {}
}

function showError(message) {
  status.textContent = message || 'This public app could not be opened.'
  status.className = 'is-error'
}

function httpError(response) {
  const error = new Error(`Public storage request failed (${response.status}).`)
  error.status = response.status
  return error
}

const encodePath = (path) => String(path ?? '').split('/').map(encodeURIComponent).join('/')

// Answers the runtime's storage RPC with the public storage endpoints (reads
// of public/, writes inside the declared write_prefix, CAS) and answers its
// housekeeping calls (drain, signal queue, subscribe) benignly so an anonymous
// session never crashes on a stubbed error.
async function handleStorageRpc(message) {
  const requestId = message.requestId
  if (!requestId) return
  const method = typeof message.method === 'string' ? message.method : ''
  const args = Array.isArray(message.args) ? message.args : []
  const base = '/api/public-storage/'
  const ok = (result) => send({ type: 'moebius:storage-rpc-result', requestId, ok: true, result })
  const fail = (error) => send({
    type: 'moebius:storage-rpc-result',
    requestId,
    ok: false,
    error: {
      name: error?.name || 'Error',
      message: error?.message || 'Public storage request failed.',
      code: error?.code,
      status: error?.status,
      retryable: Boolean(error?.retryable),
    },
  })
  try {
    if (method === 'get' || method === 'getText') {
      const response = await fetch(base + encodePath(args[0]), { headers: authHeaders })
      if (response.status === 404) return ok(null)
      if (!response.ok) throw httpError(response)
      return ok(method === 'getText' ? await response.text() : await response.json())
    }
    if (method === 'getWithVersion') {
      const kind = args[1] || 'json'
      const response = await fetch(base + encodePath(args[0]), { headers: authHeaders })
      if (response.status === 404) return ok({ value: null, version: null })
      if (!response.ok) throw httpError(response)
      const value = kind === 'text' ? await response.text() : await response.json()
      return ok({ value, version: response.headers.get('ETag') })
    }
    if (method === 'list') {
      const options = args[1] || {}
      const url = '/api/public-storage?prefix=' + encodeURIComponent(args[0] || '')
        + (options.includeContent ? '&include_content=true' : '')
      const response = await fetch(url, { headers: authHeaders })
      if (!response.ok) throw httpError(response)
      const data = await response.json()
      return ok(Array.isArray(data.entries) ? data.entries : [])
    }
    if (method === 'durableWrite') {
      const [path, data, options = {}] = args
      const headers = { ...authHeaders }
      headers['Content-Type'] = options.kind === 'text' ? 'text/plain; charset=utf-8' : 'application/json'
      if (options.ifMatch) headers['If-Match'] = options.ifMatch
      if (options.ifNoneMatch === true) headers['If-None-Match'] = '*'
      const body = options.kind === 'text' ? String(data ?? '') : JSON.stringify(data)
      const response = await fetch(base + encodePath(path), { method: 'PUT', headers, body })
      if (response.status === 412) {
        const error = new Error('Public storage precondition failed.')
        error.code = 'conflict'
        error.status = 412
        error.retryable = true
        throw error
      }
      if (!response.ok) throw httpError(response)
      return ok({ durability: 'synced', path, version: response.headers.get('ETag') })
    }
    if (method === 'remove') {
      const response = await fetch(base + encodePath(args[0]), { method: 'DELETE', headers: authHeaders })
      if (!response.ok && response.status !== 404) throw httpError(response)
      return ok({})
    }
    if (method === 'subscribe') {
      const [subscriptionId, path, kind = 'json'] = args
      fetch(base + encodePath(path), { headers: authHeaders }).then(async (response) => {
        let value = null
        if (response.ok) value = kind === 'text' ? await response.text() : await response.json()
        send({ type: 'moebius:storage-change', subscriptionId, value })
      }).catch(() => {})
      return ok(true)
    }
    if (method === 'unsubscribe') return ok(true)
    if (method === 'pendingCount' || method === 'pendingSignalCount') return ok(0)
    if (method === 'queueSignals' || method === 'drainSignals' || method === 'drain') return ok(true)
    const error = new Error('This storage operation is unavailable in a public session.')
    error.code = 'public_storage_unsupported'
    error.status = 403
    throw error
  } catch (error) {
    fail(error)
  }
}

function handleModuleRequest(message) {
  if (!message.requestId || String(message.appId) !== String(APP_ID)) return
  send({ type: 'moebius:module-ack', requestId: message.requestId, appId: APP_ID })
  const retry = message.retry === 1 ? '&retry=1' : ''
  fetch(`/api/public-apps/${APP_ID}/module?v=${encodeURIComponent(VERSION)}${retry}`, {
    headers: authHeaders,
  })
    .then(async (response) => {
      if (!response.ok) {
        const error = new Error(`The app module returned ${response.status}.`)
        error.status = response.status
        throw error
      }
      return response.arrayBuffer()
    })
    .then((bytes) => send({
      type: 'moebius:module-result', requestId: message.requestId,
      appId: APP_ID, ok: true, bytes,
    }, [bytes]))
    .catch((error) => send({
      type: 'moebius:module-result', requestId: message.requestId,
      appId: APP_ID, ok: false,
      error: { code: 'module-load-failed', message: error.message, status: error.status || null },
    }))
}

// The page is always the visible app, and the contract it received names only
// the capabilities this host can provide, so every other request is rejected
// by the host as undeclared.
const capabilities = createCapabilityHost({
  providers: {
    [DEVICE_STORAGE]: createDeviceStorageProvider({
      appId: APP_ID,
      getIdentity: () => ({ appId: APP_ID, appInstanceId: APP_INSTANCE }),
    }),
  },
  getDeclaration: (capability) => CAPABILITY_CONTRACT.runtime?.[capability] || null,
  isActive: () => true,
  send(source, message, transfer) {
    try { source.postMessage(message, '*', transfer) } catch {}
  },
})

frame.addEventListener('load', () => send({
  type: 'moebius:frame-init', token: TOKEN, themeCss: '', bg: BACKGROUND,
  storage: {}, capabilityContract: CAPABILITY_CONTRACT,
}))
frame.src = FRAME_URL

window.addEventListener('message', (event) => {
  if (event.source !== frame.contentWindow) return
  if (event.origin !== 'null' && event.origin !== window.location.origin) return
  const message = event.data
  if (!message || typeof message !== 'object') return

  if (message.type === 'moebius:module-request') {
    handleModuleRequest(message)
    return
  }
  if (message.type === 'moebius:storage-rpc') {
    handleStorageRpc(message)
    return
  }
  if (capabilities.handle(event.source, message)) return
  if (message.type === 'moebius:frame-mounted' && String(message.appId) === String(APP_ID)) {
    status.className = 'is-ready'
    return
  }
  if (message.type === 'moebius:frame-error') {
    showError(message.error?.message || message.message)
    return
  }
  if (message.type === 'moebius:token-expired' || message.type === 'moebius:token-refresh-request') {
    window.location.reload()
  }
})
