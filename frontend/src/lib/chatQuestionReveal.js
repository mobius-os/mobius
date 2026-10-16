// Transient state for bringing a chat's one actionable question into view.
// A notification can ask an already-mounted chat to reveal that question. This
// is intentionally transient: it must not replace the owner's saved reading
// position or replay after a reload.
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

// Contract R5c: a newly pending decision also comes into view by itself, at
// most once per page session. Remembering the decision each chat has already
// shown is what lets a reader scroll away from it without being pulled back.
// Like notification requests this is transient: a reload forgets it, which at
// most brings a still-pending decision into view one more time.
const arrivalReveals = new Map()

export function questionArrivalRevealed(chatId, questionId) {
  const id = String(chatId || '')
  return !!id && !!questionId && arrivalReveals.get(id) === String(questionId)
}

export function markQuestionArrivalRevealed(chatId, questionId) {
  const id = String(chatId || '')
  if (!id || !questionId) return
  arrivalReveals.set(id, String(questionId))
}
