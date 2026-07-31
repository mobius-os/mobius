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
  if (record.stack || record.is_stack) {
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

/** The one-line status word shown on a card the chat can act on. */
export function statusLabel(record) {
  if (record?.status === 'submitting') return 'Publishing'
  if (record?.stack || record?.is_stack) return 'Review together'
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

/**
 * Reduce git's multi-line per-file table to the aggregate final line for the
 * docked summary card. Full file details remain available in the disclosure.
 */
export function diffStatSummary(value) {
  if (typeof value !== 'string') return ''
  const lines = value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  return lines.at(-1) || ''
}

/**
 * Copy for the grouped panel across its whole transition, not just its pending
 * records. A sent acknowledgement is still a visible row, so it must count
 * toward the grouping decision until that acknowledgement leaves.
 */
export function reviewPanelSummary(pendingCount, sentCount) {
  const pending = Math.max(0, Number(pendingCount) || 0)
  const sent = Math.max(0, Number(sentCount) || 0)
  const count = pending + sent

  if (sent === 0) {
    return {
      count,
      title: `${pending} ${pending === 1 ? 'review' : 'reviews'} ready`,
      copy: 'Each item keeps its own review and action.',
    }
  }
  if (pending === 0) {
    return {
      count,
      title: `${sent} item${sent === 1 ? '' : 's'} contributed`,
      copy: 'Each item was contributed separately.',
    }
  }
  return {
    count,
    title: `${pending} remaining · ${sent} contributed`,
    copy: 'Each item keeps its own review and action.',
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
    item => {
      if (isDismissed(reviewItemDismissIdentity(item), storage)) return false
      // Chat is the quick happy path. If a single contribution needs repair,
      // Contribute remains its durable home; rendering a disabled attention
      // card above the composer only creates a dead end. Stacks are different:
      // their one useful chat action is to open the ordered review in Contribute.
      if (item.kind === 'record') {
        return !sendBlocker(item.record, {
          connected: payload?.connected !== false,
        })
      }
      return true
    },
  )
}

export function rememberReviewItemDismissed(item, storage) {
  return rememberDismissed(reviewItemDismissIdentity(item), storage)
}
