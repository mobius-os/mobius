/**
 * One-shot "reveal this message" intents from drawer chat search.
 *
 * The drawer, on a search-result tap, records which transcript row the opened
 * chat should jump to (keyed by chat id), then navigates normally. The mounted
 * ChatView for that chat consumes the intent once its transcript is on screen:
 * it scrolls the matched row into view (through the scroll controller, so the
 * machine owns the position) and flashes a brief highlight.
 *
 * A plain module singleton — this is transient navigation glue, not persisted
 * state. Nothing survives a reload (a reload lands on the reader's last saved
 * scroll position, which is the sensible default). `key` is the row's
 * `data-key` (`<role>-<ts>`); ts is unique within a chat.
 */

const _pending = new Map() // chatId -> { key, terms }
const _listeners = new Set() // (chatId) => void

/** Record a jump target and notify any already-mounted ChatView for that chat
 *  (the re-selected-while-open case). Pass a null/empty key to no-op. `terms`
 *  are the matched surface forms to highlight within the message (optional). */
export function requestSearchReveal(chatId, key, terms = []) {
  if (chatId == null || !key) return
  const id = String(chatId)
  _pending.set(id, { key, terms: Array.isArray(terms) ? terms : [] })
  for (const fn of _listeners) {
    try { fn(id) } catch { /* a listener error must not drop the intent */ }
  }
}

/** Read the pending target for a chat without consuming it. */
export function peekSearchReveal(chatId) {
  return _pending.get(String(chatId)) || null
}

/** Read and clear the pending target for a chat. */
export function takeSearchReveal(chatId) {
  const id = String(chatId)
  const value = _pending.get(id) || null
  if (value) _pending.delete(id)
  return value
}

/** Subscribe to reveal requests (fires with the target chat id). Returns an
 *  unsubscribe function. */
export function subscribeSearchReveal(listener) {
  _listeners.add(listener)
  return () => _listeners.delete(listener)
}
