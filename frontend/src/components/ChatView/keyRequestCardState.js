/* Pure state for a saved key card (useKeyRequestCard.js); unit-tested. */

export const KEY_ALLOW = 'allow'
export const KEY_DENY = 'deny'

/**
 * What the card shows for vault request ``id``: ``pending`` (answerable),
 * ``denied`` or ``gone`` (answered in the vault already: an allowed request
 * leaves it), ``guest`` (not the owner), ``loading``, ``unavailable`` (the
 * vault is not running), ``error``, or ``idle`` for a card that is not
 * live (answered elsewhere, replaced by a later message, or history): it
 * reads nothing and shows only its choices.
 */
export function keyRequestCardState({ id, owner, live = true, keys = {} }) {
  if (!owner) return { state: 'guest', request: null }
  if (!live) return { state: 'idle', request: null }
  if (keys.error) return { state: 'error', request: null }
  if (!keys.data) return { state: 'loading', request: null }
  if (!keys.data.available) return { state: 'unavailable', request: null }
  const request = (keys.data.requests || []).find(item => item.id === id) || null
  if (!request) return { state: 'gone', request: null }
  if (request.status === 'denied') return { state: 'denied', request }
  return { state: request.status === 'pending' ? 'pending' : 'gone', request }
}

/**
 * Whether an answer of ``kind`` already matches the vault in ``state``, so
 * submitting the card needs no vault call (a retried submit, or an answer
 * given before a reload). Null when the vault disagrees.
 */
export function keyAnswerAlreadyGiven(state, kind) {
  if (state === 'pending') return false
  if (state === 'denied') return kind === KEY_DENY ? true : null
  if (state === 'gone') return kind === KEY_ALLOW ? true : null
  return null
}
