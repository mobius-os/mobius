// Pure model for the chat's contribution review card (view in
// ContributionReviewCard.jsx). Every decision the card makes about what to show
// and whether Send may be offered lives here, so the rules are unit-testable
// without a DOM and the component stays a renderer.
//
// The card is a SECOND view over the ledger the Contribute app owns. It never
// relaxes a gate: Send calls the same submit endpoint as the app's button, and
// that endpoint re-runs every freshness, attribution, and fork check before it
// pushes anything public. Nothing here can authorize a publish on its own.

// The ledger lives in the Contribute app's storage, so the card resolves that
// app by slug. Not installed → nothing staged → the card never renders.
export const CONTRIBUTE_SLUG = 'contribute'

// Records the owner can still act on. Settled ones (open/merged/closed) are
// history and belong in the Contribute app, not above the composer.
export const ACTIONABLE_STATUSES = new Set(['prepared', 'submitting'])

export function contributeAppId(apps) {
  const match = (apps || []).find(app => app && app.slug === CONTRIBUTE_SLUG)
  return match ? Number(match.id) : null
}

export function contributeApp(apps, appId) {
  return (apps || []).find(app => app && Number(app.id) === appId) || null
}

export function actionableRecords(payload) {
  return (payload?.records || []).filter(
    record => ACTIONABLE_STATUSES.has(record?.status),
  )
}

/**
 * Why this record cannot be sent right now, or null when it can be.
 *
 * The server already ran the same local preflight the Contribute app's review
 * cards use; this only turns its verdict into one owner-facing sentence. A
 * missing verdict fails closed. The submit endpoint remains authoritative, but
 * this one-tap public action must not claim that an absent review is ready.
 */
export function sendBlocker(record, { connected } = {}) {
  if (!record || record.status !== 'prepared') return null
  if (record.is_stack) {
    return 'This is one layer of a stacked set — review and send the whole chain in Contribute.'
  }
  if (connected === false) return 'Connect GitHub in Contribute first.'
  const review = record.review
  if (!review) return 'Open Contribute to review this before sending.'
  if (review.state === 'ready') return null
  return review.message
    || 'This needs to be prepared again before it can be sent.'
}

/**
 * Whether a successful send should grant the background review-response loop.
 *
 * Mirrors the Contribute app's Send: the owner's stored default, and only when
 * the backend advertises the capability. An unreadable/absent default means ON,
 * exactly as the app treats it, so the two surfaces cannot disagree about what a
 * press authorizes.
 */
export function autopilotOnSend(payload) {
  if (!payload || payload.autopilot_available !== true) return false
  return payload.autopilot_default !== false
}

// The platform's own repository. A contribution to it reaches every Möbius
// owner, which is what the card's action and payoff line can honestly promise;
// an app's repository reaches that app's users instead.
export const PLATFORM_REPO = 'mobius-os/mobius'

/** The one-line status word shown on the card. */
export function statusLabel(record, blocked) {
  if (record?.status === 'submitting') return 'Publishing'
  if (blocked) return 'Needs an update'
  return 'Ready to contribute'
}

/**
 * The action label.
 *
 * "Contribute" carries the value of the act where "Send for review" only named
 * the mechanism, and it avoids "upstream" — precise to anyone who works with
 * open source, and opaque to everyone else. The destination is only named when
 * we actually know it is the platform, so the button can never point someone at
 * the wrong project.
 */
export function contributeLabel(record) {
  return record?.repo === PLATFORM_REPO
    ? 'Contribute to Möbius'
    : 'Contribute this improvement'
}

/**
 * One quiet line making the payoff concrete.
 *
 * It carries the motivation the button cannot, and stays honest about the
 * review: acceptance is the maintainers' call, not a consequence of the tap.
 */
export function payoffLine(record) {
  return record?.repo === PLATFORM_REPO
    ? "If it's accepted, everyone running Möbius gets this."
    : "If it's accepted, everyone using this app gets it."
}

// ── Swipe-to-dismiss ────────────────────────────────────────────────────────
//
// Dismissing is a VIEW decision, never a data one: the contribution stays
// prepared in the ledger and the Contribute app still lists it. A swipe is easy
// to perform by accident, so it must never be able to drop staged work.
//
// The gesture geometry deliberately mirrors the navigation drawer's swipe-close
// (see lib/drawerLifecycle.js). Both live in their own module for now rather
// than sharing one: the drawer's copy is in review upstream, and consolidating
// them here would collide with that. Worth folding into one shared predicate
// once it lands.

// Travel before a touch counts as a horizontal intent at all.
export const SWIPE_SLOP_PX = 10
// How decisively sideways it must be. Anything less belongs to the vertical
// scroller inside an expanded card.
export const SWIPE_DOMINANCE = 1.15
// Travel past which a release dismisses instead of snapping back.
export const DISMISS_DX_PX = 64

/** True only when this displacement is decisively sideways, either direction. */
export function isHorizontalSwipe(dx, dy) {
  return Math.abs(dx) > SWIPE_SLOP_PX
    && Math.abs(dx) > Math.abs(dy) * SWIPE_DOMINANCE
}

/** True when a release at this displacement should dismiss the card. */
export function passedDismissThreshold(dx, dy) {
  return isHorizontalSwipe(dx, dy) && Math.abs(dx) >= DISMISS_DX_PX
}

/**
 * The dismissal identity of a record.
 *
 * Keyed on the record's last update as well as its id, so dismissing means "not
 * this version". If the agent re-stages the contribution — new code, new
 * wording — there is a fresh decision to make and the card comes back. Without
 * the timestamp a single swipe would bury every later revision of the same work.
 */
export function dismissKey(record) {
  if (!record?.id) return null
  return `mobius:contrib-dismissed:${record.id}:${record.updated_at || ''}`
}

export function isDismissed(record, storage) {
  const key = dismissKey(record)
  if (!key || !storage) return false
  try {
    return storage.getItem(key) === '1'
  } catch {
    return false
  }
}

export function rememberDismissed(record, storage) {
  const key = dismissKey(record)
  if (!key || !storage) return false
  try {
    storage.setItem(key, '1')
    return true
  } catch {
    // Private browsing or a full quota: the card simply reappears next time,
    // which is the safe direction to fail.
    return false
  }
}

/** Records that still deserve the composer's attention. */
export function visibleRecords(payload, storage) {
  return actionableRecords(payload).filter(record => !isDismissed(record, storage))
}
