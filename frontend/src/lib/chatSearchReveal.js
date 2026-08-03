// A drawer result is navigation plus one short-lived instruction for the
// transcript already mounted for that chat. Keep it out of URL/state
// persistence: a search jump must not replace the owner's saved reading
// position or reappear after a reload.
const reveals = new Map()
const listeners = new Map()
const expiryTimers = new Map()
let serial = 0
export const CHAT_SEARCH_REVEAL_TTL_MS = 30_000

function notify(chatId) {
  for (const listener of listeners.get(String(chatId)) || []) listener()
}

/** Keep a successfully consumed reveal captured for this ChatView activation.
 * Removing it from the live store must not re-run activation with the owner's
 * saved anchor, while expiry of an unconsumed reveal still cancels it. */
export function reconcileChatSearchActivation(current, chatId, liveReveal) {
  const id = String(chatId || '')
  if (!current || current.chatId !== id) {
    return { chatId: id, reveal: liveReveal || null, consumedId: null }
  }
  if (liveReveal) {
    if (current.reveal?.id === liveReveal.id) return current
    return { chatId: id, reveal: liveReveal, consumedId: null }
  }
  if (current.reveal && current.consumedId !== current.reveal.id) {
    return { chatId: id, reveal: null, consumedId: null }
  }
  return current
}

export function consumeChatSearchActivation(current, revealId) {
  if (!current?.reveal || current.reveal.id !== revealId) return current
  if (current.consumedId === revealId) return current
  return { ...current, consumedId: revealId }
}

export function requestChatSearchReveal(chatId, { anchorKey, terms } = {}) {
  const id = String(chatId || '')
  if (!id || typeof anchorKey !== 'string' || !anchorKey) return null
  clearTimeout(expiryTimers.get(id))
  const reveal = {
    id: ++serial,
    anchorKey,
    terms: Array.isArray(terms) ? [...terms] : [],
    expiresAt: Date.now() + CHAT_SEARCH_REVEAL_TTL_MS,
  }
  reveals.set(id, reveal)
  const timer = setTimeout(() => {
    if (reveals.get(id)?.id !== reveal.id) return
    reveals.delete(id)
    expiryTimers.delete(id)
    notify(id)
  }, CHAT_SEARCH_REVEAL_TTL_MS)
  timer?.unref?.()
  expiryTimers.set(id, timer)
  notify(id)
  return reveal
}

export function chatSearchRevealFor(chatId) {
  const id = String(chatId || '')
  const reveal = reveals.get(id) || null
  if (reveal && reveal.expiresAt <= Date.now()) {
    reveals.delete(id)
    clearTimeout(expiryTimers.get(id))
    expiryTimers.delete(id)
    return null
  }
  return reveal
}

export function clearChatSearchReveal(chatId, revealId = null) {
  const id = String(chatId || '')
  const current = reveals.get(id)
  if (!current || (revealId != null && current.id !== revealId)) return false
  reveals.delete(id)
  clearTimeout(expiryTimers.get(id))
  expiryTimers.delete(id)
  notify(id)
  return true
}

export function subscribeChatSearchReveal(chatId, listener) {
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
