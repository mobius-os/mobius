/* Pure state for a saved key card (useKeyRequestCard.js); unit-tested. */

export const KEY_ALLOW = 'allow'
export const KEY_DENY = 'deny'

/** Deep equality of plain JSON values, whatever the order of object keys. */
function sameValue(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((item, i) => sameValue(item, b[i]))
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const keys = Object.keys(a)
    return keys.length === Object.keys(b).length && keys.every(key => sameValue(a[key], b[key]))
  }
  return a === b
}

/**
 * What the card shows for vault request ``id``: ``pending`` (answerable),
 * ``denied`` (refused in the vault already), ``gone`` (no longer waiting as
 * this card asked it: allowed, removed in Settings, or since asked again with
 * other sites), ``guest`` (not the owner), ``loading``, ``unavailable`` (the
 * vault is not running), ``error``, or ``idle`` for a card that is not live
 * (answered elsewhere, replaced by a later message, or history): it reads
 * nothing and shows only its choices. ``sites`` is what the card asked for
 * (null on a card saved before cards kept it). Whether a ``gone`` card's
 * answer is true the server decides from the key itself.
 */
export function keyRequestCardState({ id, sites = null, owner, live = true, keys = {} }) {
  if (!owner) return { state: 'guest', request: null }
  if (!live) return { state: 'idle', request: null }
  if (keys.error) return { state: 'error', request: null }
  if (!keys.data) return { state: 'loading', request: null }
  if (!keys.data.available) return { state: 'unavailable', request: null }
  const request = (keys.data.requests || []).find(item => item.id === id) || null
  const asked = request && (!Array.isArray(sites) || sameValue(request.sites || [], sites))
  if (!asked || (request.status !== 'pending' && request.status !== 'denied')) return { state: 'gone', request: null }
  return { state: request.status, request }
}

/**
 * Whether an answer of ``kind`` already matches the vault in ``state``, so
 * submitting the card needs no vault call (a retried submit, or an answer
 * given before a reload). Null when the vault disagrees.
 */
export function keyAnswerAlreadyGiven(state, kind) {
  if (state === 'pending') return false
  if (state === 'denied') return kind === KEY_DENY ? true : null
  // Nothing to answer in the vault; the server admits only the true answer.
  if (state === 'gone') return true
  return null
}
