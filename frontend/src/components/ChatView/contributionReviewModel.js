// Pure model for the chat's contribution review card (view in
// ContributionReviewCard.jsx). The card keeps the reviewed happy path in the
// conversation where the work happened and opens Contribute for deeper review,
// stacks, or recovery. Both surfaces call the same platform-owned mutations.

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

/** Why direct publication is unavailable, or null when the exact review can send. */
export function sendBlocker(record, { connected } = {}) {
  if (!record || record.status !== 'prepared') return null
  if (record.stack || record.is_stack) {
    return 'Review and send this linked set together in Contribute.'
  }
  if (connected === false) return 'Connect GitHub in Contribute before sending.'
  if (record.quality_review_ready !== true) {
    return 'Finish the exact agent review before sending.'
  }
  if (record.review?.state === 'ready') return null
  return record.review?.message
    || 'Refresh this review in Contribute before sending.'
}

/** Match Contribute's default follow-up grant for a newly opened PR. */
export function autopilotOnSend(payload) {
  return payload?.autopilot_available === true
    && payload.autopilot_default !== false
}

/** Copy for the one public action represented by this prepared record. */
export function publicationAction(record) {
  return record?.action === 'pr_update'
    ? { label: 'Update PR', busyLabel: 'Updating PR', progress: 'Updating the reviewed pull request…' }
    : { label: 'Send PR', busyLabel: 'Sending PR', progress: 'Opening the reviewed pull request…' }
}

// The platform repository remains useful in tests and record grouping, but the
// card no longer changes its action copy by repository: every target opens the
// same exact Contribute review contract.
export const PLATFORM_REPO = 'mobius-os/mobius'

const RECORD_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

/** The opaque app intent for one exact contribution review. */
export function contributionReviewIntent(record) {
  const id = typeof record?.id === 'string' ? record.id.trim() : ''
  return RECORD_ID.test(id) ? `review:${id}` : null
}

/** A stack opens through one of its records; Contribute resolves the whole unit. */
export function reviewItemIntent(item) {
  if (item?.kind === 'record') return contributionReviewIntent(item.record)
  if (item?.kind !== 'stack') return null
  return contributionReviewIntent(item.records?.[0])
}

/** The one-line status word shown on a card the chat can act on. */
export function statusLabel(record) {
  if (record?.status === 'submitting') return 'Publishing'
  if (typeof record?.last_submit_error === 'string' && record.last_submit_error.trim()) {
    return 'Needs attention'
  }
  if (record?.stack || record?.is_stack) return 'Review together'
  if (record?.quality_review_ready === true && record?.review?.state === 'ready') {
    return record?.action === 'pr_update' ? 'Ready to update' : 'Ready to send'
  }
  return 'Review ready'
}

export function reviewDestinationLabel(record) {
  if (record?.status === 'submitting') return 'View in Contribute'
  if (typeof record?.last_submit_error === 'string' && record.last_submit_error.trim()) {
    return 'Resolve in Contribute'
  }
  if (record?.stack || record?.is_stack) return 'Review stack in Contribute'
  return 'Review in Contribute'
}

/**
 * Reduce git's multi-line per-file table to the aggregate final line for the
 * docked summary card. Full file details remain in the selected Contribute review.
 */
export function diffStatSummary(value) {
  if (typeof value !== 'string') return ''
  const lines = value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  return lines.at(-1) || ''
}

/**
 * The failure this card should currently explain, or null when there is none.
 *
 * A send that failed is a fact about the record, not about this render, so the
 * compact doorway still explains it after a reload. Contribute owns retrying.
 */
export function submitFailure(record, { attempt = null, sending = false } = {}) {
  if (sending) return null
  const source = attempt || {
    message: record?.last_submit_error,
    detail: record?.last_submit_error_detail,
  }
  const message = typeof source.message === 'string' ? source.message.trim() : ''
  if (!message) return null
  const detail = typeof source.detail === 'string' ? source.detail.trim() : ''
  return { message, detail }
}

/** Copy for the grouped panel while it still has pending work. */
export function reviewPanelSummary(pendingCount) {
  const count = Math.max(0, Number(pendingCount) || 0)
  return {
    count,
    title: 'Reviews ready',
    copy: 'Each opens at its exact decision in Contribute.',
  }
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

function stackDescriptor(record) {
  const stackId = record?.stack?.id
  if (typeof stackId === 'string' && stackId.trim()) return record.stack
  // Frontend source hot-reloads while backend Python waits for a restart. Keep
  // that real rolling-upgrade seam coherent by recognizing the canonical stack
  // branch emitted by older loaded backends; the next backend start supplies
  // the explicit descriptor above. Unknown legacy shapes stay safely ungrouped.
  if (!record?.is_stack || typeof record?.branch !== 'string') return null
  const match = record.branch.match(/^stack\/([^/]+)\/(\d+)(?:-|$)/)
  if (!match) return null
  const id = match[1]
  return {
    id,
    name: id.split('-').filter(Boolean)
      .map((part, index) => index === 0
        ? part.charAt(0).toUpperCase() + part.slice(1)
        : part)
      .join(' '),
    position: Number(match[2]),
    total: null,
  }
}

/** Collapse every valid contribution stack into one logical review item. */
export function reviewItems(payload) {
  const items = []
  const stacks = new Map()
  for (const record of actionableRecords(payload)) {
    const stack = stackDescriptor(record)
    if (!stack) {
      items.push({ kind: 'record', id: record.id, record })
      continue
    }
    const groupKey = `${record?.repo || ''}:${stack.id}`
    let item = stacks.get(groupKey)
    if (!item) {
      item = {
        kind: 'stack',
        id: `stack:${groupKey}`,
        stack,
        repo: record.repo,
        records: [],
      }
      stacks.set(groupKey, item)
      items.push(item)
    }
    item.records.push(record)
  }
  for (const item of stacks.values()) {
    item.records.sort((left, right) => {
      const a = stackDescriptor(left)?.position
      const b = stackDescriptor(right)?.position
      const hasA = typeof a === 'number' && Number.isFinite(a)
      const hasB = typeof b === 'number' && Number.isFinite(b)
      if (hasA && hasB) return a - b
      if (hasA) return -1
      if (hasB) return 1
      return String(left?.id || '').localeCompare(String(right?.id || ''))
    })
  }
  return items
}

function reviewItemDismissIdentity(item) {
  if (item?.kind === 'record') return item.record
  if (item?.kind !== 'stack') return null
  return {
    id: item.id,
    // Any revised layer makes this a fresh stack decision, even when a newer
    // sibling still has the group's greatest timestamp.
    updated_at: item.records
      .map(record => `${record.id}:${record.updated_at || ''}`)
      .sort()
      .join('|'),
  }
}

export function visibleReviewItems(payload, storage) {
  return reviewItems(payload).filter(
    item => !isDismissed(reviewItemDismissIdentity(item), storage),
  )
}

export function rememberReviewItemDismissed(item, storage) {
  return rememberDismissed(reviewItemDismissIdentity(item), storage)
}
