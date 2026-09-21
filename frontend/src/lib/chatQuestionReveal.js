// A notification can ask an already-mounted chat to reveal its one actionable
// question. This is intentionally transient: it must not replace the owner's
// saved reading position or replay after a reload.
const requests = new Map()
const listeners = new Map()
let serial = 0

function notify(chatId) {
  for (const listener of listeners.get(chatId) || []) listener()
}

export function requestChatQuestionReveal(chatId) {
  const id = String(chatId || '')
  if (!id) return null
  const request = { id: ++serial }
  requests.set(id, request)
  notify(id)
  return request
}

export function chatQuestionRevealFor(chatId) {
  return requests.get(String(chatId || '')) || null
}

export function consumeChatQuestionReveal(chatId, requestId) {
  const id = String(chatId || '')
  if (requests.get(id)?.id !== requestId) return false
  requests.delete(id)
  notify(id)
  return true
}

export function subscribeChatQuestionReveal(chatId, listener) {
  const id = String(chatId || '')
  let set = listeners.get(id)
  if (!set) {
    set = new Set()
    listeners.set(id, set)
  }
  set.add(listener)
  return () => {
    set.delete(listener)
    if (set.size === 0) listeners.delete(id)
  }
}
