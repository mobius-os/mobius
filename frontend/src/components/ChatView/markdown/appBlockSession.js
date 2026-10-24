/* Pure state for a passive transcript block session (building-apps.md
   "Passive block sessions"). The app's hidden frame reports bounded text,
   links and control state; it never supplies markup or authority. The host
   owns which controls exist and which owner event may be sent. */

import { safeSourceUrl } from '../messageSources.js'
import { blockBadges, shortText } from './appBlock.js'

export const SESSION_LIMITS = Object.freeze({
  actions: 24, label: 40, status: 40, note: 500, notice: 500, summary: 240,
  links: 12, linkLabel: 120, checkpointId: 128, checkpointBytes: 32768, nonce: 128,
  confirmationItems: 256, confirmationFacts: 8, confirmationText: 512,
})
const SESSION_TONES = new Set(['neutral', 'success', 'attention', 'danger'])
const RECOVERY_ERROR = 'This app session needs recovery. Open the app to check the result before trying again.'
export const INCOMPLETE_CONFIRMATION = 'Open the app to review this action. Its full current identity is unavailable here.'
export const AMBIGUOUS_CONFIRMATION = 'Open the app to review these actions. More than one is asking for confirmation at once.'

const checkpointId = value => typeof value === 'string' && value.length >= 1 && value.length <= SESSION_LIMITS.checkpointId
const tone = value => SESSION_TONES.has(value) ? value : 'neutral'

/** Opaque handover data is never interpreted here. Copy only its two fields
 *  and bound UTF-8 bytes. `undefined` means present but invalid. */
export function sessionCheckpoint(value) {
  if (value === null) return null
  if (!value || typeof value !== 'object' || Array.isArray(value) || !checkpointId(value.id)
    || typeof value.data !== 'string'
    || new TextEncoder().encode(value.data).length > SESSION_LIMITS.checkpointBytes) return undefined
  return { id: value.id, data: value.data }
}

/** Confirmation is the app's complete presentation of the exact action it
 *  will take. Unlike decoration it is never shortened: any oversized or
 *  malformed member rejects the whole list, which keeps Confirm disabled. */
export function confirmationOf(value) {
  const text = raw => typeof raw === 'string' && raw.trim() !== '' && raw.length <= SESSION_LIMITS.confirmationText
  if (!Array.isArray(value) || value.length === 0 || value.length > SESSION_LIMITS.confirmationItems) return null
  const items = []
  for (const item of value) {
    if (!text(item?.title) || !Array.isArray(item.facts) || item.facts.length > SESSION_LIMITS.confirmationFacts
      || item.facts.some(fact => !text(fact?.label) || !text(fact?.value))) return null
    items.push({ title: item.title, facts: item.facts.map(({ label, value: factValue }) => ({ label, value: factValue })) })
  }
  return items
}

function sessionAction(raw) {
  // A hidden control cannot be the one the owner confirms.
  const confirming = raw.confirming === true && raw.hidden !== true
  const confirmation = confirming ? confirmationOf(raw.confirmation) : null
  const incomplete = confirming && !confirmation
  return {
    key: raw.key, label: shortText(raw.label, SESSION_LIMITS.label),
    disabled: raw.disabled === true || incomplete, busy: raw.busy === true, confirming, confirmation,
    hidden: raw.hidden === true,
    note: incomplete ? INCOMPLETE_CONFIRMATION : shortText(raw.note, SESSION_LIMITS.note),
    tone: tone(raw.tone), status: shortText(raw.status, SESSION_LIMITS.status), statusTone: tone(raw.statusTone),
    links: (Array.isArray(raw.links) ? raw.links : []).slice(0, SESSION_LIMITS.links)
      .map(link => ({ label: shortText(link?.label, SESSION_LIMITS.linkLabel), url: safeSourceUrl(link?.url, { httpsOnly: true }) }))
      .filter(link => link.label && link.url),
    // Omission keeps the saved snapshot's badges; an empty list clears them.
    ...(Array.isArray(raw.badges) ? { badges: blockBadges(raw.badges) } : {}),
  }
}

/** Parse one app-block-state message for this session and its known keys. */
export function sessionState(message, sessionId, keys) {
  if (!message || message.type !== 'moebius:app-block-state' || message.sessionId !== sessionId
    || !Array.isArray(message.actions) || message.actions.length > SESSION_LIMITS.actions) return null
  const checkpoint = Object.hasOwn(message, 'checkpoint') ? sessionCheckpoint(message.checkpoint) : null
  const seen = new Set()
  const actions = []
  for (const raw of message.actions) {
    if (!raw || !keys.has(raw.key) || seen.has(raw.key)) continue
    seen.add(raw.key)
    actions.push(sessionAction(raw))
  }
  // One confirmation at a time: the host shows one app-sourced list beside
  // one Confirm. Several confirming actions are ambiguous, so none can confirm.
  if (actions.filter(action => action.confirming).length > 1) {
    for (const action of actions) {
      if (!action.confirming) continue
      Object.assign(action, { confirmation: null, disabled: true, note: AMBIGUOUS_CONFIRMATION })
    }
  }
  return {
    actions, notice: shortText(message.notice, SESSION_LIMITS.notice), summary: shortText(message.summary, SESSION_LIMITS.summary),
    retain: message.retain === true || Boolean(checkpoint), checkpoint: checkpoint ?? null,
    checkpointInvalid: checkpoint === undefined, recoveryError: message.recoveryError === true ? RECOVERY_ERROR : null,
    checkpointAck: checkpointId(message.checkpointAck) ? message.checkpointAck : null,
    ackNonce: typeof message.ackNonce === 'string' && message.ackNonce.length <= SESSION_LIMITS.nonce ? message.ackNonce : null,
  }
}

function recoveryHold(previous, incoming, error = previous?.checkpoint && !previous?.recoveryError ? null : RECOVERY_ERROR) {
  const actions = (incoming?.actions?.length ? incoming.actions : previous?.actions || [])
    .map(action => ({ ...action, disabled: true, busy: false, confirming: false, confirmation: null,
      hidden: false, note: error || RECOVERY_ERROR }))
  return { ...incoming, actions, checkpoint: previous?.checkpoint ?? null, retain: true, recoveryPending: true,
    recoveryError: error, notice: error || RECOVERY_ERROR, ackNonce: null }
}

/** Keep the hidden document alive while it owns anything unresolved: an
 *  unacknowledged owner event, a confirmation, a busy action, a checkpoint,
 *  an explicit retain or a pending recovery. Idle sessions are released. */
export function sessionRetained(state, event) {
  return Boolean((event && state?.ackNonce !== event.nonce) || state?.retain || state?.checkpoint
    || state?.recoveryPending || state?.actions.some(action => action.busy || action.confirming))
}

/** A new document cannot own the previous document's controls or frozen
 *  confirmation. Unresolved ownership is held, disabled, until the new
 *  document acknowledges the exact checkpoint; anything else is discarded. */
export function sessionDocumentReset(state, event = null) {
  const pendingConfirm = event?.event === 'confirm' && state?.ackNonce !== event.nonce
  const busy = state?.actions.some(action => action.busy)
  const idleConfirmation = state?.actions.some(action => action.confirming) && !busy
  if (!state?.checkpoint && !state?.recoveryPending && !busy && !pendingConfirm
    && (!state?.retain || idleConfirmation)) return null
  return recoveryHold(state, null)
}

/** Apply parsed state. During recovery only an exact checkpoint
 *  acknowledgement (observational, not approval) releases the hold. */
export function sessionStateUpdate(previous, safe) {
  if (!safe) return previous
  if (safe.checkpointInvalid || safe.recoveryError) return recoveryHold(previous, safe, RECOVERY_ERROR)
  if (previous?.recoveryPending && !(previous.checkpoint && safe.checkpointAck === previous.checkpoint.id)) {
    return recoveryHold(previous, safe)
  }
  return { ...safe, retain: safe.retain || Boolean(safe.checkpoint), recoveryPending: false, recoveryError: null }
}

/** The snapshot to show beside live state. While the app asks for
 *  confirmation, only badges the app itself supplies are shown. */
export function sessionSnapshot(block, state) {
  const confirming = Boolean(state?.actions.some(action => action.confirming))
  const live = key => state?.actions.find(action => action.key === key)?.badges
  const badges = (saved, key) => live(key) ?? (confirming ? [] : saved)
  return { ...block, badges: badges(block.badges, block.action?.intent),
    items: block.items.map(item => ({ ...item, badges: badges(item.badges, item.action?.intent) })) }
}

/** Passive execution needs the owner's accepted grant and the server's
 *  admission of the exact applied bundle; block JSON can never ask for it. */
export function passiveAppBlockAllowed(app) {
  return app?.capability_contract?.runtime?.['chat.blocks.passive']?.version === 1
    && typeof app.passive_block_module_digest === 'string'
    && /^[a-f0-9]{64}$/.test(app.passive_block_module_digest)
}
