// Pure review and publication model for the explicit Changes surface.

import { contributionNeedsAttention } from './chatChangesLifecycle.js'

const CONTRIBUTE_SLUG = 'contribute'
const ACTIONABLE_STATUSES = new Set(['prepared', 'submitting'])
const TRACKING_STATUSES = new Set([
  'draft', 'open', 'landing', 'merged', 'superseded', 'closed',
])
const CHAT_VISIBLE_STATUSES = new Set([
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

function chatContributionRecords(payload) {
  return (payload?.records || []).filter(
    record => CHAT_VISIBLE_STATUSES.has(record?.status),
  )
}

function actionableReviewRecords(payload) {
  return chatContributionRecords(payload).filter(
    record => ACTIONABLE_STATUSES.has(record?.status)
      || contributionNeedsAttention(record),
  )
}

function isTrackingRecord(record) {
  return TRACKING_STATUSES.has(record?.status)
}

/** Why direct publication is unavailable, or null when the exact review can send. */
export function sendBlocker(record, { connected } = {}) {
  const resumableSuccessor = record?.status === 'submitting'
    && record?.successor === true
  if (record?.status === 'submitting' && !resumableSuccessor) {
    return 'This GitHub action is still being confirmed.'
  }
  if (!record || (record.status !== 'prepared' && !resumableSuccessor)) return null
  if (!resumableSuccessor
    && typeof record.last_submit_error === 'string'
    && record.last_submit_error.trim()) {
    return 'This contribution needs a fresh check before it can continue.'
  }
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

export function isUpdateAction(action) {
  return action === 'pr_update'
}

/** Copy for the one public action represented by this prepared record. */
export function publicationAction(record) {
  if (record?.status === 'submitting' && record?.successor === true) {
    return { label: 'Resume update', busyLabel: 'Resuming update' }
  }
  return isUpdateAction(record?.action)
    ? { label: 'Update PR', busyLabel: 'Updating PR' }
    : { label: 'Send PR', busyLabel: 'Sending PR' }
}

export function publicationMutations(record) {
  if (record?.successor === true) {
    return [
      'Force-update the pull request branch to the reviewed successor commit',
      'Retarget the pull request base branch to the surviving branch',
    ]
  }
  if (isUpdateAction(record?.action)) {
    return ['Update the pull request branch with the reviewed commit']
  }
  return ['Open a new pull request for the reviewed branch']
}

/** Why one complete reviewed stack cannot use its guarded public action yet. */
export function stackSendBlocker(item, { connected } = {}) {
  if (item?.kind !== 'stack') return 'This is not a complete contribution stack.'
  const records = Array.isArray(item.records) ? item.records : []
  const total = Number(item.stack?.total)
  const positions = new Set(records.map(record => Number(record?.stack?.position)))
  if (!Number.isInteger(total) || total < 2
    || records.length !== total || positions.size !== total
    || !Array.from({ length: total }, (_, index) => index + 1)
      .every(position => positions.has(position))) {
    return 'The complete linked set needs another review before it can continue.'
  }
  if (records.some(record => record?.status === 'submitting')) {
    return 'This linked GitHub action is still being confirmed.'
  }
  if (connected === false) return 'Connect GitHub in Contribute before sending.'
  const prepared = records.filter(record => record?.status === 'prepared')
  if (prepared.length === 0) return 'This contribution stack has no private action waiting.'
  const actions = new Set(prepared.map(record => record?.action || 'pr'))
  if (actions.size !== 1) return 'The linked set needs one consistent public action.'
  for (const record of prepared) {
    if (typeof record.last_submit_error === 'string' && record.last_submit_error.trim()) {
      return 'This contribution stack needs a fresh check before it can continue.'
    }
    if (record.quality_review_ready !== true) {
      return 'The agent review is still in progress.'
    }
    if (record.review?.state !== 'ready') {
      return record.review?.message || 'Refresh this review before sending.'
    }
  }
  return null
}

export function publicationStackAction(item) {
  const prepared = (item?.records || []).filter(record => record?.status === 'prepared')
  const updating = prepared.length > 0
    && prepared.every(record => record?.action === 'pr_update')
  return {
    label: updating ? 'Update stack' : 'Send stack',
    confirmLabel: updating ? 'Update PRs' : 'Send PRs',
    count: prepared.length,
    updating,
  }
}

/** Exact public actions represented by one confirmation, regardless of grouping. */
export function publicationItemsAction(items) {
  const records = (Array.isArray(items) ? items : []).flatMap(item => (
    item?.kind === 'stack'
      ? (item.records || []).filter(record => record?.status === 'prepared')
      : [item?.record].filter(Boolean)
  ))
  const updating = records.length > 0
    && records.every(record => isUpdateAction(record?.action))
  const verb = updating ? 'Update' : 'Send'
  return {
    count: records.length,
    updating,
    promptLabel: `${verb} ${records.length} reviewed pull ${records.length === 1 ? 'request' : 'requests'}?`,
    confirmLabel: `${verb} ${records.length} ${records.length === 1 ? 'PR' : 'PRs'}`,
  }
}

export function publicationItemsMutations(items) {
  const records = (Array.isArray(items) ? items : []).flatMap(item => (
    item?.kind === 'stack'
      ? (item.records || []).filter(record => record?.status === 'prepared')
      : [item?.record].filter(Boolean)
  ))
  return [...new Set(records.flatMap(publicationMutations))]
}

const RECORD_ID = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

/** The opaque app intent for one exact contribution review. */
export function contributionReviewIntent(record) {
  const id = typeof record?.id === 'string' ? record.id.trim() : ''
  return RECORD_ID.test(id) ? `review:${id}` : null
}

const OWNER_FAILURE_CODES = new Set([
  'github_not_connected', 'missing_github_token', 'forbidden',
  'insufficient_permission', 'permission_denied',
])

/** Decide who owns a failed public action after the server reconciles it. */
export function publicationFailureOwner(failure) {
  const status = Number(failure?.status)
  const code = String(failure?.code || '').toLowerCase()
  if (status === 401 || status === 403 || OWNER_FAILURE_CODES.has(code)) return 'owner'
  return 'agent'
}

/** One accepted-action identity; a changed review naturally returns. */
export function reviewActionKey(record) {
  if (!record?.id) return ''
  if (typeof record.action_key === 'string' && record.action_key) {
    return `${record.id}:${record.action_key}`
  }
  if (isTrackingRecord(record) && contributionNeedsAttention(record)) {
    return [
      record.id,
      record.status || '',
      record.needs_attention === true ? 'attention' : '',
      record.last_submit_error || '',
      record.review?.code || '',
    ].join(':')
  }
  return `${record.id}:${record.updated_at || ''}:${record.status || ''}`
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
  const records = actionableReviewRecords(payload)
  const chatRecordIds = new Set(chatContributionRecords(payload).map(record => record.id))
  const represented = new Set()
  for (const unit of Array.isArray(payload?.stack_units) ? payload.stack_units : []) {
    const unitRecords = Array.isArray(unit?.records) ? unit.records : []
    if (!unitRecords.some(record => chatRecordIds.has(record?.id))) continue
    const stack = unitRecords.map(stackDescriptor).find(Boolean)
    if (!stack) continue
    const item = {
      kind: 'stack',
      id: `stack:${unit.repo || ''}:${unit.id || stack.id}`,
      stack: { ...stack, name: unit.name || stack.name },
      repo: unit.repo || unitRecords[0]?.repo,
      records: [...unitRecords].sort((left, right) => (
        (stackDescriptor(left)?.position || 0) - (stackDescriptor(right)?.position || 0)
      )),
    }
    item.records.forEach(record => represented.add(record.id))
    items.push(item)
  }
  for (const record of records) {
    if (represented.has(record.id)) continue
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

/**
 * Resolve a cached approval against the latest ledger without silently
 * changing what the owner approved. A changed record action makes the old
 * confirmation obsolete, so callers can render the refreshed decision.
 */
export function currentReviewItems(expectedItems, payload) {
  const expected = Array.isArray(expectedItems) ? expectedItems : []
  const current = new Map(reviewItems(payload).map(item => [item.id, item]))
  const actionKeys = item => item?.kind === 'stack'
    ? (item.records || []).map(reviewActionKey).sort()
    : item?.kind === 'record'
      ? [reviewActionKey(item.record)]
      : []
  const resolved = expected.map(item => current.get(item?.id))
  if (resolved.some(item => !item)) return null
  const unchanged = expected.every((item, index) => (
    actionKeys(item).join('|') === actionKeys(resolved[index]).join('|')
  ))
  return unchanged ? resolved : null
}

/** Keep the owner's selected units visible when their reviewed heads move.
 * Callers must show the refreshed actions and ask for a new confirmation;
 * this deliberately never authorizes the newer heads on its own. */
export function refreshedReviewItems(expectedItems, payload) {
  const current = new Map(reviewItems(payload).map(item => [item.id, item]))
  return (Array.isArray(expectedItems) ? expectedItems : [])
    .map(item => current.get(item?.id))
    .filter(Boolean)
}
