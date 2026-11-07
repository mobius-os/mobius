/* Pure state for a passive transcript block session (building-apps.md
   "Passive block sessions"). The app's hidden frame reports bounded text,
   links and control state; it never supplies markup or authority. The host
   owns which controls exist and which owner event may be sent. */

import { safeSourceUrl } from '../messageSources.js'
import { appBlockHref, appIntent, blockBadges, shortText } from './appBlock.js'

export const SESSION_LIMITS = Object.freeze({
  actions: 24, label: 40, status: 40, note: 500, notice: 500, summary: 240,
  links: 12, linkLabel: 120, checkpointId: 128, checkpointBytes: 32768, nonce: 128,
  confirmationItems: 256, confirmationFacts: 8, confirmationText: 512,
  title: 120, items: 12, itemStatus: 40, itemNote: 200, itemLinks: 2, itemLinkLabel: 40,
  rows: 12, rowTitle: 120, rowMeta: 160,
})
const SESSION_TONES = new Set(['neutral', 'success', 'attention', 'danger', 'accent'])
/** What this host draws from a session report beyond the original fields,
 *  advertised in `blockSession.features` and `app-block-init`: `row-state`
 *  covers `title`, `items`, `itemFacts`, `open` and the `accent` tone;
 *  `rows-v2` adds app-sourced `rows`, `titleTone`/`titleBusy`, the host's
 *  `context` (chat id, newest block) and `moebius:app-block-refresh` hints.
 *  An app that does not see a feature is on an older host and should keep its
 *  older presentation. */
export const SESSION_FEATURES = Object.freeze(['row-state', 'rows-v2'])
const RECOVERY_ERROR = 'This app session needs recovery. Open the app to check the result before trying again.'
export const INCOMPLETE_CONFIRMATION = 'Open the app to review this action. Its full current identity is unavailable here.'
export const AMBIGUOUS_CONFIRMATION = 'Open the app to review these actions. More than one is asking for confirmation at once.'

const sessionLinks = (value, max, labelMax) => (Array.isArray(value) ? value : []).slice(0, max)
  .map(link => ({ label: shortText(link?.label, labelMax), url: safeSourceUrl(link?.url, { httpsOnly: true }) }))
  .filter(link => link.label && link.url)

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
    links: sessionLinks(raw.links, SESSION_LIMITS.links, SESSION_LIMITS.linkLabel),
    // An action that can do nothing here may instead open the block in the
    // app; it sends no event, so it can never confirm.
    open: raw.open === true && !confirming,
    // Omission keeps the saved snapshot's badges; an empty list clears them.
    ...(Array.isArray(raw.badges) ? { badges: blockBadges(raw.badges) } : {}),
  }
}

/** Live state for the block's listed rows, keyed by item intent: a short
 *  status (a pill), a busy mark, one short note and up to two links. Unknown
 *  intents are dropped and the first report for an intent wins. Decoration
 *  only: a row's state never adds a control or changes what Confirm does. */
export function sessionItems(value, intents) {
  const items = new Map()
  for (const raw of (Array.isArray(value) ? value : []).slice(0, SESSION_LIMITS.items)) {
    if (!raw || !intents.has(raw.intent) || items.has(raw.intent)) continue
    const item = { status: shortText(raw.status, SESSION_LIMITS.itemStatus), statusTone: tone(raw.statusTone),
      busy: raw.busy === true, note: shortText(raw.note, SESSION_LIMITS.itemNote),
      links: sessionLinks(raw.links, SESSION_LIMITS.itemLinks, SESSION_LIMITS.itemLinkLabel) }
    if (item.status || item.busy || item.note || item.links.length) items.set(raw.intent, item)
  }
  return items
}

/** App-sourced rows (`rows-v2`): the app's own list for the block, replacing
 *  the saved items and facts for display. Each row is a title, one quiet
 *  metadata line, an optional pill (`pill`, `tone`, `busy`), up to two
 *  `https:` links and an optional intent its title opens in the app. Rows
 *  never carry a control. `null` means the report has no rows (the saved
 *  snapshot stays); an empty list means the app shows no rows. Rows without
 *  a title are dropped; the list is cut at 12. */
export function sessionRows(value, app) {
  if (!Array.isArray(value)) return null
  const rows = []
  for (const raw of value.slice(0, SESSION_LIMITS.rows)) {
    const title = shortText(raw?.title, SESSION_LIMITS.rowTitle)
    if (!title) continue
    const intent = appIntent(raw.intent)
    rows.push({ title, meta: shortText(raw.meta, SESSION_LIMITS.rowMeta),
      ...(intent && app ? { intent, href: appBlockHref(app, intent) } : {}),
      live: { status: shortText(raw.pill, SESSION_LIMITS.itemStatus), statusTone: tone(raw.tone), busy: raw.busy === true,
        note: '', links: sessionLinks(raw.links, SESSION_LIMITS.itemLinks, SESSION_LIMITS.itemLinkLabel) } })
  }
  return rows
}

/** Parse one app-block-state message for this session, its known action keys
 *  and (optionally) the block's item intents. */
export function sessionState(message, sessionId, keys, intents = new Set(), app = '') {
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
    title: shortText(message.title, SESSION_LIMITS.title), items: sessionItems(message.items, intents),
    titleTone: tone(message.titleTone), titleBusy: message.titleBusy === true, rows: sessionRows(message.rows, app),
    itemFacts: message.itemFacts === 'values' ? 'values' : '',
    retain: message.retain === true || Boolean(checkpoint), checkpoint: checkpoint ?? null,
    checkpointInvalid: checkpoint === undefined, recoveryError: message.recoveryError === true ? RECOVERY_ERROR : null,
    checkpointAck: checkpointId(message.checkpointAck) ? message.checkpointAck : null,
    ackNonce: typeof message.ackNonce === 'string' && message.ackNonce.length <= SESSION_LIMITS.nonce ? message.ackNonce : null,
  }
}

function recoveryHold(previous, incoming, error = previous?.checkpoint && !previous?.recoveryError ? null : RECOVERY_ERROR) {
  const actions = (incoming?.actions?.length ? incoming.actions : previous?.actions || [])
    .map(action => ({ ...action, disabled: true, busy: false, confirming: false, confirmation: null,
      hidden: false, open: false, note: error || RECOVERY_ERROR }))
  return { ...incoming, actions, title: '', titleTone: 'neutral', titleBusy: false, items: new Map(), rows: null, itemFacts: '', checkpoint: previous?.checkpoint ?? null, retain: true, recoveryPending: true,
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
 *  document acknowledges the exact checkpoint; anything else is discarded.
 *  Only `confirm` may cause an effect (building-apps.md "Passive block
 *  sessions"), so an unacknowledged `confirm` is held, while an
 *  unacknowledged `activate` or `cancel`, which change nothing, is not. */
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

/** The snapshot to show beside live state. While one action is confirming
 *  with a complete list, that confirmation owns the whole block: the app's
 *  items are the only rows (marked `confirmation`, linking nowhere), the
 *  saved facts and every other control are gone, and the confirming action
 *  is drawn once, as the block's own action. Otherwise, while anything is
 *  confirming, only badges the app itself supplies are shown.
 *
 *  App-sourced `rows` follow the same single-owner rule outside a complete
 *  confirmation: they replace every saved item and root fact (so no saved
 *  row or item control is drawn beside them), and only the block's own
 *  action remains. While any action is confirming without a complete list,
 *  the saved snapshot is shown instead, exactly as without rows. */
export function sessionSnapshot(block, state) {
  const confirming = state?.actions.find(action => action.confirming)
  const live = key => state?.actions.find(action => action.key === key)?.badges
  if (confirming?.confirmation) {
    const target = [block.action, ...block.items.map(item => item.action)].find(action => action?.intent === confirming.key)
    return { ...block, title: state.title || block.title, badges: live(confirming.key) ?? [], facts: [], action: target || null, confirmation: true,
      items: confirming.confirmation.map(item => ({ title: item.title, facts: item.facts, badges: [], action: null, confirmation: true })) }
  }
  const badges = (saved, key) => live(key) ?? (confirming ? [] : saved)
  const heading = { title: state?.title || block.title, liveTitle: Boolean(state?.title),
    titleTone: state?.title ? state.titleTone || 'neutral' : 'neutral', titleBusy: Boolean(state?.title && state.titleBusy) }
  if (state?.rows && !confirming) {
    return { ...block, ...heading, appRows: true, facts: [], badges: badges(block.badges, block.action?.intent),
      items: state.rows.map(row => ({ title: row.title, href: row.href || '', meta: row.meta, facts: [], badges: [], action: null, appRow: true, live: row.live })) }
  }
  // The app's live title and row state replace the saved snapshot's; an
  // open confirmation (above) shows only the app's list.
  return { ...block, ...heading,
    itemFacts: state?.itemFacts || block.itemFacts, badges: badges(block.badges, block.action?.intent),
    items: block.items.map(item => ({ ...item, badges: badges(item.badges, item.action?.intent),
      live: confirming ? null : state?.items?.get(item.intent) || null })) }
}

/** Passive execution needs the owner's accepted grant and the server's
 *  admission of the exact applied bundle; block JSON can never ask for it. */
export function passiveAppBlockAllowed(app) {
  return app?.capability_contract?.runtime?.['chat.blocks.passive']?.version === 1
    && typeof app.passive_block_module_digest === 'string'
    && /^[a-f0-9]{64}$/.test(app.passive_block_module_digest)
}
