export function createProviderSwitchId() {
  return globalThis.crypto?.randomUUID?.()
    || `switch-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function providerSwitchPayload({
  provider,
  model,
  effort,
  effortByProvider,
  switchId,
}) {
  return {
    provider,
    switch_id: switchId,
    agent_settings_json: {
      model,
      effort,
      effort_by_provider: effortByProvider,
    },
  }
}

export function restorableProviderSwitch(saved, chatId, sourceProvider) {
  if (
    !saved
    || saved.chatId !== chatId
    || saved.sourceProvider !== sourceProvider
  ) {
    return null
  }
  return saved
}

export async function providerSwitchResponseData(
  response,
  { provider, switchId } = {},
) {
  try {
    const data = await response.json()
    if (
      !data
      || typeof data !== 'object'
      || data.protocol !== 'provider-switch-v1'
      || data.switch_id !== switchId
      || data.provider !== provider
    ) {
      return null
    }
    return data
  } catch {
    // A 2xx status with a truncated/unreadable body is ambiguous: the server
    // may already have committed. The caller must retain the switch_id and
    // retry rather than pretending it received authoritative provider state.
    return null
  }
}

const STORE_VERSION = 1
const IDLE_STATE = Object.freeze({
  status: 'idle',
  request: null,
  error: '',
  result: null,
})
const states = new Map()
const listeners = new Map()

function storageKey(chatId) {
  return `mobius:provider-switch:v${STORE_VERSION}:${chatId}`
}

function persistedState(chatId) {
  try {
    const raw = globalThis.sessionStorage?.getItem(storageKey(chatId))
    if (!raw) return IDLE_STATE
    const saved = JSON.parse(raw)
    if (
      saved?.version !== STORE_VERSION
      || saved.chatId !== chatId
      || !saved.request
    ) return IDLE_STATE
    // An in-memory request survives ChatView navigation and keeps its true
    // ``switching`` state. Reaching this path means the document reloaded, so
    // the original fetch can no longer report completion. Keep the stable id
    // for an idempotent retry and describe the outcome as ambiguous.
    if (saved.status === 'switching') {
      return {
        status: 'error',
        request: saved.request,
        error: 'The switch may have completed. Retry to confirm its state.',
        result: null,
      }
    }
    return {
      status: saved.status === 'error' ? 'error' : 'confirming',
      request: saved.request,
      error: typeof saved.error === 'string' ? saved.error : '',
      result: null,
    }
  } catch {
    return IDLE_STATE
  }
}

function persist(chatId, state) {
  try {
    if (!state.request || state.status === 'idle' || state.status === 'success') {
      globalThis.sessionStorage?.removeItem(storageKey(chatId))
      return
    }
    globalThis.sessionStorage?.setItem(storageKey(chatId), JSON.stringify({
      version: STORE_VERSION,
      chatId,
      status: state.status,
      request: state.request,
      error: state.error,
    }))
  } catch { /* private browsing / storage quota */ }
}

export function getProviderSwitchState(chatId) {
  if (!chatId) return IDLE_STATE
  if (!states.has(chatId)) states.set(chatId, persistedState(chatId))
  return states.get(chatId)
}

export function subscribeProviderSwitch(chatId, listener) {
  if (!chatId) return () => {}
  let chatListeners = listeners.get(chatId)
  if (!chatListeners) {
    chatListeners = new Set()
    listeners.set(chatId, chatListeners)
  }
  chatListeners.add(listener)
  return () => {
    chatListeners.delete(listener)
    if (chatListeners.size === 0) listeners.delete(chatId)
  }
}

function setProviderSwitchState(chatId, next) {
  if (!chatId) return IDLE_STATE
  states.set(chatId, next)
  persist(chatId, next)
  listeners.get(chatId)?.forEach(listener => listener())
  return next
}

export function stageProviderSwitch(chatId, request) {
  return setProviderSwitchState(chatId, {
    status: 'confirming', request, error: '', result: null,
  })
}

export function beginProviderSwitch(chatId, request) {
  return setProviderSwitchState(chatId, {
    status: 'switching', request, error: '', result: null,
  })
}

export function failProviderSwitch(chatId, request, error) {
  return setProviderSwitchState(chatId, {
    status: 'error', request, error, result: null,
  })
}

export function completeProviderSwitch(chatId, request, result) {
  return setProviderSwitchState(chatId, {
    status: 'success', request, error: '', result,
  })
}

export function clearProviderSwitch(chatId) {
  return setProviderSwitchState(chatId, IDLE_STATE)
}

export function isProviderSwitchBlocking(chatId) {
  return getProviderSwitchState(chatId).status === 'switching'
}

// Test seam: simulates a new document/module while retaining sessionStorage.
export function resetProviderSwitchMemoryForTests() {
  states.clear()
  listeners.clear()
}
