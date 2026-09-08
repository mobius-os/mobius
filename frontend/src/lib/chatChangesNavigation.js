/** One-shot navigation to a chat's existing Changes surface, never an action. */
const KEY = 'mobius-chat-changes-navigation'
const MAX_AGE_MS = 60_000
const listeners = new Map()
let pending = null

function storage() {
  try { return globalThis.sessionStorage } catch { return null }
}

export function requestChatChanges(chatId) {
  if (!chatId) return
  pending = { chatId: String(chatId), expiresAt: Date.now() + MAX_AGE_MS }
  // Memory owns same-document delivery; session storage carries only this
  // short-lived destination across the standalone app's shell navigation.
  try { storage()?.setItem(KEY, JSON.stringify(pending)) } catch { /* restricted storage */ }
  for (const listener of listeners.get(pending.chatId) || []) listener()
}

export function consumeChatChanges(chatId) {
  let request = pending
  if (!request) {
    try { request = JSON.parse(storage()?.getItem(KEY) || 'null') } catch { return false }
  }
  if (!request) return false
  const valid = typeof request.expiresAt === 'number' && request.expiresAt > Date.now()
  if (valid && request.chatId !== String(chatId)) return false
  pending = null
  try { storage()?.removeItem(KEY) } catch { /* same-document state is already consumed */ }
  return valid && request.chatId === String(chatId)
}

export function subscribeChatChanges(chatId, listener) {
  const id = String(chatId)
  const callbacks = listeners.get(id) || new Set()
  callbacks.add(listener)
  listeners.set(id, callbacks)
  return () => {
    callbacks.delete(listener)
    if (!callbacks.size) listeners.delete(id)
  }
}
