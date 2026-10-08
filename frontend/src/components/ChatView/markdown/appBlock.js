/* A transcript block names an app view; it carries no authority of its own.
   Opening it shows the app's live view, which applies the app's own checks to
   anything it offers to do. */

// `proposed` is a reviewed change that is not on GitHub yet, so it has no number.
const PULL_STATES = new Set(['proposed', 'open', 'draft', 'merged', 'closed'])
const TONES = new Set(['success', 'attention', 'danger', 'accent', 'neutral'])
const SESSION_TONES = new Set(['neutral', 'success', 'attention', 'danger'])
// The complete JSON block bounds size; an app owns its opaque destination
// payload, which may name a batch rather than one short record identifier.
const INTENT = /^[a-z][a-z0-9-]*:[^\s]+$/
const shortText = (value, max) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : ''
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null

function safeHttps(value) {
  if (typeof value !== 'string' || value.length > 2048) return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

function pullBadges(value) {
  return (Array.isArray(value) ? value : [])
    .filter(badge => shortText(badge?.label, 40)).slice(0, 3)
    .map(badge => ({ label: shortText(badge.label, 40), tone: TONES.has(badge.tone) ? badge.tone : 'neutral' }))
}

// Confirmation is a complete presentation of the app's frozen action. Unlike
// historical decoration, it must never silently drop or shorten a member.
function confirmationOf(value) {
  const text = raw => typeof raw === 'string' && raw.trim() && raw.length <= 512
  if (!Array.isArray(value) || !value.length || value.length > 256) return null
  const items = []
  for (const item of value) {
    if (!text(item?.title) || !Array.isArray(item.facts) || item.facts.length > 8
      || item.facts.some(fact => !text(fact?.label) || !text(fact?.value))) return null
    items.push({ title: item.title, facts: item.facts.map(({ label, value }) => ({ label, value })) })
  }
  return items
}

/** Keep the app-owned publisher alive until the app acknowledges the event
 *  and explicitly releases any active or uncertain operation. */
export function inlineSessionRetained(state, event) {
  return Boolean(event && state?.ackNonce !== event.nonce || state?.retain
    || state?.actions.some(action => action.busy || action.confirming))
}

/** A new document cannot own the previous document's frozen confirmation.
 *  Keep unresolved publication ownership until fresh observation releases it. */
export function inlineBlockDocumentReset(state) {
  if (!state?.retain && !state?.actions.some(action => action.busy)) return null
  return { ...state, actions: state.actions.map(action => action.confirming
    ? { ...action, confirming: false, confirmation: null, disabled: true, label: 'Checking…' }
    : action) }
}

/** The iframe contributes text and links, never markup or action authority.
 *  Saved snapshots remain history; confirmation describes the current action. */
export function inlineBlockState(message, sessionId, keys) {
  if (!message || message.type !== 'moebius:app-block-state' || message.sessionId !== sessionId
    || !Array.isArray(message.actions) || message.actions.length > 24) return null
  const seen = new Set()
  const actions = []
  for (const raw of message.actions) {
    if (!raw || !keys.has(raw.key) || seen.has(raw.key)) continue
    seen.add(raw.key)
    const links = (Array.isArray(raw.links) ? raw.links : []).slice(0, 12)
      .map(link => ({ label: shortText(link?.label, 120), url: safeHttps(link?.url) }))
      .filter(link => link.label && link.url)
    const confirmation = raw.confirming === true ? confirmationOf(raw.confirmation) : null
    const incomplete = raw.confirming === true && !confirmation
    actions.push({ key: raw.key, label: shortText(raw.label, 40),
      disabled: raw.disabled === true || incomplete, busy: raw.busy === true, confirming: raw.confirming === true,
      confirmation,
      hidden: raw.hidden === true, note: incomplete ? 'Open the app to review this action. Its full current identity is unavailable here.' : shortText(raw.note, 500),
      tone: SESSION_TONES.has(raw.tone) ? raw.tone : 'neutral',
      status: shortText(raw.status, 40),
      statusTone: SESSION_TONES.has(raw.statusTone) ? raw.statusTone : 'neutral', links,
      // Omission preserves the saved snapshot; an empty list deliberately clears it.
      ...(Array.isArray(raw.badges) ? { badges: pullBadges(raw.badges) } : {}) })
  }
  return { actions, notice: shortText(message.notice, 500), summary: shortText(message.summary, 240),
    retain: message.retain === true,
    ackNonce: typeof message.ackNonce === 'string' && message.ackNonce.length <= 128 ? message.ackNonce : null }
}

/* A pull-request snapshot renders like a GitHub PR row. Anything malformed is
   dropped rather than guessed, so the block falls back to its plain facts. */
function pullSnapshot(value) {
  if (!value || typeof value !== 'object') return null
  if (typeof value.repo !== 'string' || !/^[\w.-]{1,100}\/[\w.-]{1,100}$/.test(value.repo)
    || value.repo.split('/').some(part => /^\.+$/.test(part))) return null
  if (!PULL_STATES.has(value.state)) return null
  const numbered = Number.isSafeInteger(value.number) && value.number >= 1
  if (!numbered && value.state !== 'proposed') return null
  const labels = (Array.isArray(value.labels) ? value.labels : []).slice(0, 12)
    .filter(label => typeof label?.name === 'string' && label.name.trim() && label.name.length <= 50)
    .map(label => ({ name: label.name, ...(/^[0-9a-f]{6}$/i.test(label.color || '') ? { color: label.color } : {}) }))
  const badges = pullBadges(value.badges)
  return {
    repo: value.repo, repoUrl: `https://github.com/${value.repo}`, number: numbered ? value.number : null, state: value.state, badges,
    author: typeof value.author === 'string' && /^[\w-]{1,100}(\[bot\])?$/.test(value.author) ? value.author : null,
    files: count(value.files), additions: count(value.additions), deletions: count(value.deletions),
    labels, url: numbered && safeHttps(value.url) === `https://github.com/${value.repo}/pull/${value.number}`
      ? `https://github.com/${value.repo}/pull/${value.number}` : undefined,
  }
}

export function appBlockFromToken(token) {
  if (token?.type !== 'code' || token.lang !== 'mobius-app' || token.text?.length > 16384) return null
  try {
    const value = JSON.parse(token.text)
    if (!/^[a-z][a-z0-9-]{0,63}$/.test(value.app || '')
      || typeof value.intent !== 'string' || !INTENT.test(value.intent)
      || typeof value.title !== 'string' || !value.title.trim() || value.title.length > 240) return null
    const facts = (Array.isArray(value.facts) ? value.facts : []).slice(0, 8)
      .filter(fact => typeof fact?.label === 'string' && typeof fact?.value === 'string')
      .map(fact => {
        // Invalid source links stay plain snapshot text.
        const href = safeHttps(fact.href)
        return { label: fact.label.slice(0, 80), value: fact.value.slice(0, 240), ...(href ? { href } : {}) }
      })
    const inline = value.inline !== false
    const interaction = inline && value.interaction === 'inline' ? 'inline' : null
    // An app may offer one primary action (and one per batch item). It only
    // opens the app's own view with a second intent; whatever that view then
    // does is the app's to check.
    const actionOf = raw => inline && shortText(raw?.label, 40) && INTENT.test(raw?.intent || '')
      ? { label: shortText(raw.label, 40), intent: raw.intent } : null
    const action = actionOf(value.action)
    // A batch block lists up to 12 items, each its own destination, under one
    // shared action (for example "Contribute all").
    const href = intent => `/shell/?${new URLSearchParams({ app: value.app, intent })}`
    const items = (Array.isArray(value.items) ? value.items : [])
      .filter(item => shortText(item?.title, 240) && INTENT.test(item?.intent || ''))
      .slice(0, 12)
      .map(item => ({ title: item.title.trim().slice(0, 240), intent: item.intent, pull: pullSnapshot(item.pull), href: href(item.intent), action: actionOf(item.action) }))
    return { app: value.app, intent: value.intent, title: value.title, facts, pull: pullSnapshot(value.pull),
      inline, interaction, action, items,
      expandLabel: shortText(value.expand_label, 40),
      height: Math.max(240, Math.min(640, Number(value.height) || 480)),
      href: href(value.intent) }
  } catch { return null }
}
