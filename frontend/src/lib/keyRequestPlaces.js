/*
 * Where a key request card shows besides Settings › Keys: in the chat whose
 * agent asked (a `mobius-key-request` fence in its reply) and over the app
 * that asked while its window is open. Pure and unit-tested;
 * KeyRequestBlock.jsx and AppKeyRequestCard.jsx render it. A card only ever
 * answers through the owner-only routes, so nothing here grants anything.
 */
import { requestFormKey } from './keysSettingsText.js'

export const KEY_REQUEST_FENCE = 'mobius-key-request'
// `{"id":"agent-<key>"}` is well under this; anything longer is not a block.
const MAX_BLOCK_JSON = 256
// A vault request id: `agent-<key>` for the owner's agents, `<app id>-<key>`
// for an app, with a key name as the vault validates it.
const REQUEST_ID = /^(agent|[0-9]+)-[A-Za-z][A-Za-z0-9_-]{0,63}$/

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
 * - ``guest``: the viewer is not the owner, so it only says a key was asked for.
 * - ``loading``, ``unavailable`` (the vault is not running) or ``error``.
 * - ``denied``: the owner said no, here or elsewhere.
 * - ``elsewhere``: another chat's agent asked; read-only, answered in Settings.
 * - ``repeated``: a newer copy of this card further down the chat answers it.
 * - ``pending``: the live card.
 * - ``gone``: allowed or withdrawn elsewhere (an allowed request leaves the vault).
 *
 * ``chatId`` is the chat the card is shown in (null outside one); a request
 * from an app, or one stored before requests named their chat, fits any chat.
 */
export function chatKeyRequestView({ id, chatId = null, owner, keys = {}, answered = null, latest = true }) {
  if (answered) return { state: 'answered', request: answered.request, kind: answered.kind }
  if (!owner) return { state: 'guest', request: null }
  if (keys.error) return { state: 'error', request: null }
  if (!keys.data) return { state: 'loading', request: null }
  if (!keys.data.available) return { state: 'unavailable', request: null }
  const request = (keys.data.requests || []).find(item => item.id === id) || null
  if (!request) return { state: 'gone', request: null }
  if (request.status === 'denied') return { state: 'denied', request }
  if (request.status !== 'pending') return { state: 'gone', request: null }
  if (request.chat_id && chatId && request.chat_id !== chatId) return { state: 'elsewhere', request }
  return { state: latest ? 'pending' : 'repeated', request }
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
