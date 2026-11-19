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

// The vault's own site rules (keyvault.py host_matches, _pattern_within): a
// `*.` host covers its subdomains, and a path pattern, which may use only `*`,
// covers a narrower one whose text it matches.
function hostCovers(pattern, host) {
  return pattern.startsWith('*.') ? host.endsWith(pattern.slice(1)) && host !== pattern.slice(2) : host === pattern
}
function pathCovers(allowed, path) {
  const glob = allowed.split('*').map(part => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*')
  return path === allowed || new RegExp(`^${glob}$`, 's').test(path)
}

/**
 * Whether the owner's agents may already send key ``name`` to every one of
 * ``sites``, as the server checks a card's Allow (key_request_cards.py
 * check_answer): the key exists, agents are allowed, and its own sites cover
 * each host, method and path asked for.
 */
export function agentsMayUse(keys, name, sites) {
  const record = (keys || []).find(key => key.name === name)
  if (!record?.grants?.agents) return false
  const allowed = record.grants.sites || []
  return sites.every(want => want.methods.every(method => want.paths.every(path => allowed.some(site => (
    hostCovers(site.host, want.host) && site.methods.includes(method) && site.paths.some(pattern => pathCovers(pattern, path))
  )))))
}

/**
 * What the card shows for vault request ``id`` (for key ``key`` on
 * ``sites``, as the card asked): ``pending`` (answerable), ``denied``
 * (refused in the vault already), ``allowed`` (no longer waiting as asked,
 * and the vault already lets the agents send the key there: allowed in
 * Settings or by hand), ``gone`` (no longer waiting, and nothing allowed:
 * removed, or since asked again with other sites), ``guest`` (not the owner),
 * ``loading``, ``unavailable`` (the vault is not running), ``error``, or
 * ``idle`` for a card that is not live (answered elsewhere, replaced by a
 * later message, or history): it reads nothing and shows only its choices.
 */
export function keyRequestCardState({ id, key, sites = [], owner, live = true, keys = {} }) {
  if (!owner) return { state: 'guest', request: null }
  if (!live) return { state: 'idle', request: null }
  if (keys.error) return { state: 'error', request: null }
  if (!keys.data) return { state: 'loading', request: null }
  if (!keys.data.available) return { state: 'unavailable', request: null }
  const request = (keys.data.requests || []).find(item => item.id === id) || null
  if (request && sameValue(request.sites || [], sites) && (request.status === 'pending' || request.status === 'denied')) {
    return { state: request.status, request }
  }
  return { state: agentsMayUse(keys.data.keys, key, sites) ? 'allowed' : 'gone', request: null }
}

/**
 * Whether an answer of ``kind`` already matches the vault in ``state``, so
 * submitting the card needs no vault call (a retried submit, an answer given
 * before a reload, or one given in Settings). Null when the vault disagrees,
 * as the server would.
 */
export function keyAnswerAlreadyGiven(state, kind) {
  if (state === 'pending') return false
  if (state === 'denied' || state === 'gone') return kind === KEY_DENY ? true : null
  if (state === 'allowed') return kind === KEY_ALLOW ? true : null
  return null
}
