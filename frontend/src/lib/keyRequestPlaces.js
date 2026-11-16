/*
 * Where a key request card shows besides Settings › Keys: in the chat whose
 * agent asked (a `mobius-key-request` fence in its reply) and over the app
 * that asked while its window is open. Pure and unit-tested;
 * KeyRequestBlock.jsx and AppKeyRequestCard.jsx render it. A card only ever
 * answers through the owner-only routes, so nothing here grants anything.
 */
import { keyAnswerWords, requestFormKey } from './keysSettingsText.js'

export const KEY_REQUEST_FENCE = 'mobius-key-request'
// `{"id":"agent-<key>"}` is well under this; anything longer is not a block.
const MAX_BLOCK_JSON = 256
// A vault request id: `agent-<key>` for the owner's agents, `<app id>-<key>`
// for an app, with a key name as the vault validates it.
const REQUEST_ID = /^(agent|[0-9]+)-[A-Za-z][A-Za-z0-9_-]{0,63}$/
const AGENT_PREFIX = 'agent-'

/**
 * ``{ id }`` for a fence that names one key request, else null, so a fence
 * with anything else (a bad id, another field, broken JSON) stays an
 * ordinary code block. The block carries no wording: the card reads the
 * request itself from the vault.
 */
export function keyRequestBlockFromToken(token) {
  if (token?.type !== 'code' || token.lang !== KEY_REQUEST_FENCE
    || typeof token.text !== 'string' || token.text.length > MAX_BLOCK_JSON) return null
  let value
  try { value = JSON.parse(token.text) } catch { return null }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const fields = Object.keys(value)
  if (fields.length !== 1 || fields[0] !== 'id') return null
  return typeof value.id === 'string' && REQUEST_ID.test(value.id) ? { id: value.id } : null
}

/**
 * What a chat's card for request ``id`` shows, as ``{ state, request }``:
 *
 * - ``answered``: this card's own answer (``answered`` is ``{ request, kind }``).
 * - ``guest``: the viewer is not the owner, so it only says who asked
 *   (``asker`` is ``'agent'`` or ``'app'``, from the id).
 * - ``loading``, ``unavailable`` (the vault is not running) or ``error``.
 * - ``denied``: the owner said no, here or elsewhere.
 * - ``app``: an app asked; read-only, answered over the app or in Settings.
 *   A chat only ever answers its own agent's requests, and its answer
 *   message is for that agent.
 * - ``elsewhere``: another chat's agent asked; read-only, answered in Settings.
 * - ``repeated``: a newer copy of this card further down the chat answers it.
 * - ``pending``: the live card.
 * - ``gone``: allowed or withdrawn elsewhere (an allowed request leaves the
 *   vault); ``goneKeyRequestLine`` may still tell which.
 *
 * ``chatId`` is the chat the card is shown in (null outside one); an agent's
 * request stored before requests named their chat fits any chat.
 */
export function chatKeyRequestView({ id, chatId = null, owner, keys = {}, answered = null, latest = true }) {
  if (answered) return { state: 'answered', request: answered.request, kind: answered.kind }
  if (!owner) return { state: 'guest', request: null, asker: id.startsWith(AGENT_PREFIX) ? 'agent' : 'app' }
  if (keys.error) return { state: 'error', request: null }
  if (!keys.data) return { state: 'loading', request: null }
  if (!keys.data.available) return { state: 'unavailable', request: null }
  const request = (keys.data.requests || []).find(item => item.id === id) || null
  if (!request) return { state: 'gone', request: null }
  if (request.status === 'denied') return { state: 'denied', request }
  if (request.status !== 'pending') return { state: 'gone', request: null }
  if (request.requester !== 'agent') return { state: 'app', request }
  if (request.chat_id && chatId && request.chat_id !== chatId) return { state: 'elsewhere', request }
  return { state: latest ? 'pending' : 'repeated', request }
}

/** The key an agent's request ``id`` names, or null for an app's request. */
export function agentRequestKey(id) {
  return id.startsWith(AGENT_PREFIX) ? id.slice(AGENT_PREFIX.length) : null
}

// The vault's audit outcome when the owner answers an agent's request.
const AGENT_ANSWERS = { 'granted agents': 'approve', 'denied agents': 'deny' }

/**
 * What a ``gone`` card for the agent's request ``id`` can still say after a
 * reload, from the key's recent activity (``usage``, newest first) and the
 * keys (``GET /api/keys``): the owner's latest answer to the agents on that
 * key, or null when it can't be known (an app's request, no answer on
 * record, an allowed key since deleted). An allowed agent may send the key
 * to the key's sites for the owner and agents, so those are the hosts named;
 * a "no" names none, since the refused sites were never saved.
 */
export function goneKeyRequestLine(id, keys, usage) {
  const key = agentRequestKey(id)
  const answer = key && (usage || []).find(entry => entry.key === key && AGENT_ANSWERS[entry.outcome])
  if (!answer) return null
  if (AGENT_ANSWERS[answer.outcome] === 'deny') return keyAnswerWords({ requester: 'agent', key, sites: [] }, 'deny', {}).line
  const record = (keys || []).find(item => item.name === key)
  return record ? keyAnswerWords({ requester: 'agent', key, sites: record.grants?.sites || [] }, 'approve', {}).line : null
}

/**
 * The pending requests an app made, newest first, for the card over that
 * app's window; ``later`` holds the requests (``requestFormKey``) the owner
 * put off until the page reloads. A replacement request under the same id
 * has a new ticket, so it shows again.
 */
export function appKeyRequestsToShow(data, appId, later = new Set()) {
  return (data?.requests || []).filter(request => request.status === 'pending'
    && request.requester !== 'agent' && String(request.app) === String(appId)
    && !later.has(requestFormKey(request)))
}

/**
 * The identity of the owner's follow-up message after answering ``request``
 * in a chat: the same request, ticket and answer always give the same
 * message id, so a retry, a second tab or a double tap posts it once.
 */
export function keyAnswerAsk(request, kind) {
  return {
    fingerprint: JSON.stringify([KEY_REQUEST_FENCE, request.id]),
    refs: [{ id: request.id, code: kind, rev: request.ticket }],
  }
}

/**
 * After the owner answers ``request`` on a chat's card, tells the asking
 * agent as one ordinary owner message through the chat's ``ask``
 * (appBlockChat.js), so its turn resumes. The message names the key and its
 * websites, never a value; its identity (keyAnswerAsk) posts it once.
 * Resolves to whether the message was sent.
 */
export function tellAgentKeyAnswer(ask, request, kind, names) {
  return ask({ message: keyAnswerWords(request, kind, names).message, ...keyAnswerAsk(request, kind) })
}
