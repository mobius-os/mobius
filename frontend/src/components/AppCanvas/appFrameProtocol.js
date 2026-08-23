import { fetchAppModuleBytes } from '../../lib/appModuleBroker.js'
import {
  clearAppFrameStorage,
  isSharedVirtualStorageKey,
  removeAppFrameStorage,
  setAppFrameStorage,
} from '../../lib/appFrameStorage.js'

export function attributedFrameVersion(frames, source) {
  for (const [version, element] of frames) {
    if (element?.contentWindow && element.contentWindow === source) return version
  }
  return null
}

const MEDIA_EVENTS = new Set(['open', 'update', 'close'])
const MEDIA_STATES = new Set(['loading', 'playing', 'paused'])

export function appMediaSessionEvent(message) {
  if (!message || message.type !== 'moebius:media-session') return null
  if (!MEDIA_EVENTS.has(message.event)) return null
  const sessionId = typeof message.sessionId === 'string' ? message.sessionId : ''
  if (!sessionId || sessionId.length > 160 || sessionId.trim() !== sessionId) return null
  if (message.event === 'close') return { event: 'close', sessionId }
  return {
    event: message.event,
    sessionId,
    title: typeof message.title === 'string'
      ? message.title.trim().slice(0, 120) || 'Playing audio'
      : 'Playing audio',
    playbackState: MEDIA_STATES.has(message.playbackState)
      ? message.playbackState
      : 'loading',
  }
}

function requestIdOf(message) {
  return typeof message.requestId === 'string' && message.requestId.length <= 160
    ? message.requestId
    : ''
}

export function serveClipboardWrite({ message, source, writeText }) {
  if (!message || message.type !== 'moebius:clipboard-write') return false
  const requestId = requestIdOf(message)
  const text = typeof message.text === 'string' && message.text.length <= 1_000_000
    ? message.text
    : ''
  if (!requestId || !text) return true
  ;(async () => {
    let ok = false
    try { ok = await writeText(text) === true } catch { /* report false */ }
    try {
      source?.postMessage({
        type: 'moebius:clipboard-write-result', requestId, ok,
      }, '*')
    } catch { /* frame detached while copying */ }
  })()
  return true
}

export function serveModuleRequest({
  message,
  source,
  appId,
  frameVersion,
  token,
  moduleUrl,
  fetchModule = fetchAppModuleBytes,
}) {
  const requestId = requestIdOf(message)
  if (!requestId || String(message.appId) !== String(appId)) return false
  const retry = message.retry === 1 ? 1 : 0

  // Acknowledge synchronously: this deadline proves host ownership, not
  // download speed. Awaiting before the ack recreates the cold-cache race.
  try {
    source?.postMessage({ type: 'moebius:module-ack', requestId, appId }, '*')
  } catch { /* detached before ownership acknowledgement */ }

  ;(async () => {
    try {
      const bytes = await fetchModule({
        baseUrl: moduleUrl, token, frameVersion, retry,
      })
      source?.postMessage({
        type: 'moebius:module-result', requestId, appId, ok: true, bytes,
      }, '*', [bytes])
    } catch (error) {
      try {
        source?.postMessage({
          type: 'moebius:module-result', requestId, appId, ok: false,
          error: {
            code: error?.code || 'module-load-failed',
            message: error?.message || 'The app module could not be loaded.',
            status: error?.status ?? null,
          },
        }, '*')
      } catch { /* detached while the request settled */ }
    }
  })()
  return true
}

export function serveStorageRpc({ message, source, host }) {
  const requestId = requestIdOf(message)
  if (!requestId) return false
  const method = typeof message.method === 'string' ? message.method : ''
  const args = Array.isArray(message.args) ? message.args : []
  ;(async () => {
    try {
      const result = await host.handleRpc(source, method, args)
      source?.postMessage({
        type: 'moebius:storage-rpc-result', requestId, ok: true, result,
      }, '*')
    } catch (error) {
      try {
        source?.postMessage({
          type: 'moebius:storage-rpc-result', requestId, ok: false,
          error: {
            name: error?.name || 'Error',
            message: error?.message || String(error),
            code: error?.code,
            status: error?.status,
            path: error?.path,
            writeId: error?.writeId,
            retryable: error?.retryable === true,
            refusedValue: error?.refusedValue,
          },
        }, '*')
      } catch { /* detached while the request settled */ }
    }
  })()
  return true
}

export function applyVirtualStorageMutation(appId, message, emitShared) {
  if (message.type === 'moebius:storage-set') {
    const saved = setAppFrameStorage(appId, message.key, message.value)
    if (saved && isSharedVirtualStorageKey(message.key)) {
      emitShared(message.key, message.value)
    }
    return true
  }
  if (message.type === 'moebius:storage-remove') {
    const removed = removeAppFrameStorage(appId, message.key)
    if (removed && isSharedVirtualStorageKey(message.key)) {
      emitShared(message.key, null)
    }
    return true
  }
  if (message.type === 'moebius:storage-clear') {
    clearAppFrameStorage(appId)
    return true
  }
  return false
}
