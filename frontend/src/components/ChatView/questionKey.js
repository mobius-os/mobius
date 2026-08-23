/**
 * Stable identity for an AskUserQuestion call across partial events.
 *
 * Two question blocks compare equal iff they represent the same
 * AskUserQuestion invocation. Prefer the SDK-assigned id (Claude
 * and Codex both supply one); fall back to the first question's
 * text so a defensive runner that omits ids still dedups correctly.
 *
 * Mirrors backend/app/events.py:question_block_key — both sides
 * must agree, otherwise the SSE stream and the persisted message
 * disagree on which existing block a new one extends and a phantom
 * card appears in the UI.
 *
 * Returns a string usable as a dict/object key.
 */
export function questionKey(block) {
  const questions = block?.questions || []
  if (block?.question_id) return `question_id:${block.question_id}`
  if (questions.length === 0) return 'empty'
  const first = questions[0] || {}
  if (first.id) return `id:${first.id}`
  return `text:${first.question || first.text || ''}`
}

/**
 * Index of the last question item, optionally constrained to one key.
 *
 * An id-less answer selects the LAST question item so a turn with two live
 * cards keys the response handoff on the card that was actually answered.
 * Both the live and catch-up patch paths and ChatView's submitted key must
 * make the same choice, so they all route through this one function rather
 * than each re-deriving it (a past divergence dropped response_activity).
 *
 * Returns -1 when no matching question item exists.
 */
export function lastQuestionIndex(items, key = null) {
  if (!Array.isArray(items)) return -1
  for (let i = items.length - 1; i >= 0; i -= 1) {
    const item = items[i]
    if (item?.type !== 'question') continue
    if (!key || questionKey(item) === key) return i
  }
  return -1
}

/** Key of the last question item, or null when there is none. */
export function lastQuestionKey(items) {
  const index = lastQuestionIndex(items)
  return index >= 0 ? questionKey(items[index]) : null
}
