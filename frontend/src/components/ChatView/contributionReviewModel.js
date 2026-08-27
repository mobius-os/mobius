// Pure model for the chat's contribution review card (view in
// ContributionReviewCard.jsx). The card keeps the reviewed happy path in the
// conversation where the work happened and opens Contribute for deeper review,
// stacks, or recovery. Both surfaces call the same platform-owned mutations.

// The ledger lives in the Contribute app's storage, so the card resolves that
// app by slug. Not installed → nothing staged → the card never renders.
export const CONTRIBUTE_SLUG = 'contribute'

// Records that still expose a publication decision in chat.
export const ACTIONABLE_STATUSES = new Set(['prepared', 'submitting'])

// Once sent, the same source chat remains the lightweight place to follow the
// contribution. Contribute owns cross-chat triage, stacks, and deeper history.
export const TRACKING_STATUSES = new Set([
  'draft', 'open', 'landing', 'merged', 'superseded', 'closed',
])

export const CHAT_VISIBLE_STATUSES = new Set([
  ...ACTIONABLE_STATUSES,
  ...TRACKING_STATUSES,
])

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

export function chatContributionRecords(payload) {
  return (payload?.records || []).filter(
    record => CHAT_VISIBLE_STATUSES.has(record?.status),
  )
}

// Chat is an action surface, not a second history feed. Healthy public and
// settled work stays in Changes and Contribute; only a decision or attention
// returns above the composer.
export function chatCardRecords(payload) {
  return chatContributionRecords(payload).filter(
    record => ACTIONABLE_STATUSES.has(record?.status)
      || record?.needs_attention === true
      || Boolean(String(record?.last_submit_error || '').trim())
      || record?.review?.state === 'needs_refresh',
  )
}

export function isTrackingRecord(record) {
  return TRACKING_STATUSES.has(record?.status)
}

export function trackingStatusLabel(record) {
  if (record?.needs_attention === true) return 'Needs attention'
  return {
    draft: 'Draft PR open',
    open: 'PR open',
    landing: 'Merging',
    merged: 'Merged',
    superseded: 'Already shared',
    closed: 'Not merged',
  }[record?.status] || 'Contribution'
}

export function trackingNarration(record) {
  if (record?.needs_attention === true) {
    return 'Checks or review need attention. Ask the agent here to sort it out.'
  }
  return {
    draft: 'The pull request is open as a draft. Its latest status stays attached to this chat.',
    open: 'The pull request is open for review. Its latest status stays attached to this chat.',
    landing: 'The verified contribution is being merged now.',
    merged: 'This improvement has landed.',
    superseded: 'Equivalent work reached the project another way.',
    closed: 'This pull request closed without merging.',
  }[record?.status] || ''
}

export function contributionFollowupPrompt(record) {
  const id = String(record?.id || '').trim()
  const title = String(record?.title || record?.summary || 'this contribution').trim()
  return [
    `Inspect and resolve the current attention on contribution ${id} ("${title}").`,
    '',
    'Refresh its GitHub and review state first, then make only the necessary local or private changes. Keep every further public update behind explicit approval in this chat.',
  ].join('\n')
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
  const code = typeof source.code === 'string'
    ? source.code
    : String(record?.last_submit_error_code || '')
  const reviewedHead = String(record?.plan?.head_sha || '')
  const exactBranchReachedGitHub = record?.last_submit_stage === 'pushed'
    && reviewedHead
    && String(record?.last_submit_push_sha || '') === reviewedHead
  if (code !== 'review_refresh_needed' && !exactBranchReachedGitHub) {
    return { message, detail }
  }
  const calmMessage = code === 'review_refresh_needed'
    ? 'The pull request changed after this review was prepared.'
    : 'Contribute could not confirm the update after the reviewed branch reached GitHub.'
  return {
    message: calmMessage,
    detail: [message, detail].filter(Boolean).join('\n\n'),
    code,
  }
}

/** The private agent intent behind the chat card's primary recovery action. */
export function contributionRecoveryDraft(record) {
  const id = String(record?.id || '').trim()
  const title = String(record?.title || 'untitled').trim()
  return [
    `Fix and review contribution ${id} ("${title}").`,
    '',
    'Refresh the recorded pull request and branch first. If the exact reviewed head already reached the pull request, reconcile the contribution record and inspect its current checks. If the branch moved, rebuild the private review on its current head and run the relevant checks.',
    '',
    'Keep any further public update behind the existing approval button.',
  ].join('\n')
}

function contributionRecoveryScope(record) {
  const id = String(record?.id || '').trim()
  if (!id) return ''
  const head = String(record?.plan?.head_sha || '')
  const input = `recovery\u0000${id}\u0000${head}`
  let hash = 0xcbf29ce484222325n
  for (let index = 0; index < input.length; index += 1) {
    hash ^= BigInt(input.charCodeAt(index))
    hash = BigInt.asUintN(64, hash * 0x100000001b3n)
  }
  return `contribute-review:${hash.toString(16).padStart(16, '0')}`
}

/** One exact failed prepared head owns one app-attributed recovery run. */
export function contributionRecoveryAction(record) {
  const scope = contributionRecoveryScope(record)
  if (!scope) return null
  return {
    title: `Fix and review ${record?.title || 'contribution'}`,
    scope,
    scopeLabel: 'Fix and review contribution',
    draft: contributionRecoveryDraft(record),
  }
}

export function contributionReviewRunPhase(runtime) {
  if (!runtime || typeof runtime !== 'object') return 'existing'
  if (runtime.running) return 'running'
  if (runtime.pending_question_id) return 'waiting'
  if (Array.isArray(runtime.pending_messages) && runtime.pending_messages.length > 0) {
    return 'running'
  }
  const goal = runtime.goal
  if (goal?.status === 'running') return 'running'
  if (goal?.status === 'paused') return 'paused'
  return 'existing'
}

/** Copy for the grouped panel while it still has pending work. */
export function reviewPanelSummary(items) {
  const list = Array.isArray(items) ? items : []
  const count = list.length
  const unsorted = list.filter(item => item?.kind === 'unsorted').length
  const tracking = list.filter(
    item => item?.kind === 'record' && isTrackingRecord(item.record),
  ).length
  if (unsorted > 0) {
    return {
      count,
      title: count === 1 ? 'Changes ready to organize' : 'Changes need you',
      copy: count === 1
        ? 'Sort reusable work into private reviews.'
        : 'Prepare new work or review what is ready.',
    }
  }
  if (tracking === count && tracking > 0) {
    return {
      count,
      title: 'Needs attention',
      copy: 'Continue the work in this chat.',
    }
  }
  if (tracking === 0) {
    return {
      count,
      title: 'Reviews ready',
      copy: 'Each opens at its exact decision in Contribute.',
    }
  }
  return {
    count,
    title: 'Contributions from this chat',
    copy: tracking === count
      ? 'Follow the latest status where the work happened.'
      : 'Review what is ready and follow what was sent.',
  }
}

/** One explicit action for a group of compact review cards. */
export function reviewGroupDefault(items, { connected } = {}) {
  const list = (Array.isArray(items) ? items : []).filter(Boolean)
  if (list.length < 2) return null
  if (list.some(item => item?.kind === 'unsorted')) return null
  if (list.some(
    item => item?.kind === 'record' && isTrackingRecord(item.record),
  )) return null
  const records = list.flatMap(item => item?.kind === 'record' ? [item.record] : [])
  const canPublishTogether = records.length === list.length && records.every(
    record => record?.status === 'prepared' && !sendBlocker(record, { connected }),
  )
  if (canPublishTogether) {
    const updates = records.filter(record => record?.action === 'pr_update').length
    return {
      kind: 'publish',
      records,
      label: updates === records.length
        ? `Update all ${records.length}`
        : `Send all ${records.length}`,
      busyLabel: `Sending 0 of ${records.length}`,
    }
  }
  return {
    kind: 'review',
    intent: 'reviews:queue',
    label: `Review all ${list.length}`,
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

/** Collapse prepared stacks; sent lifecycle records remain individually legible. */
export function reviewItems(payload) {
  const items = []
  const stacks = new Map()
  for (const record of chatCardRecords(payload)) {
    const stack = ACTIONABLE_STATUSES.has(record.status)
      ? stackDescriptor(record)
      : null
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
