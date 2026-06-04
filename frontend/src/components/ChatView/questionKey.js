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
