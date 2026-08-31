/* Typed chat-owned requests for one compact attached contribution worker. */

import {
  publicationAction,
  publicationStackAction,
  sendBlocker,
  stackSendBlocker,
} from './contributionReviewModel.js'

function cleanRevision(value) {
  return String(value || '').trim()
}

function cleanRetryOf(value) {
  return String(value || '').trim()
}

function withRetryOf(request, retryOf) {
  const value = cleanRetryOf(retryOf)
  return value ? { ...request, retry_of: value } : request
}

function recordIds(records) {
  return (Array.isArray(records) ? records : [records])
    .map(record => String(record?.id || '').trim())
    .filter(Boolean)
}

export function prepareContributionWork(revision, retryOf = '') {
  return withRetryOf({
    intent: 'prepare',
    expected_revision: cleanRevision(revision),
    record_ids: [],
  }, retryOf)
}

export function finishContributionWork(revision, retryOf = '') {
  return withRetryOf({
    intent: 'finish',
    expected_revision: cleanRevision(revision),
    record_ids: [],
  }, retryOf)
}

export function projectContributionWork(source, revision, retryOf = '') {
  return withRetryOf({
    intent: 'project',
    expected_revision: cleanRevision(revision),
    project_root: String(source?.id || '').trim(),
    record_ids: [],
  }, retryOf)
}

export function updatesContributionWork(records, revision = '', retryOf = '') {
  return withRetryOf({
    intent: 'updates',
    expected_revision: cleanRevision(revision),
    record_ids: recordIds(records),
  }, retryOf)
}

export function followupContributionWork(record, revision = '', retryOf = '') {
  return withRetryOf({
    intent: 'followup',
    expected_revision: cleanRevision(revision),
    record_ids: recordIds(record),
  }, retryOf)
}

/** Installed Contribute identity plus the one terminal helper this action supersedes. */
export function contributionWorkContext(overview) {
  const appId = Number(overview?.contributeAppId)
  return {
    appId: Number.isInteger(appId) && appId > 0 ? appId : null,
    retryOf: overview?.workState === 'attention'
      ? cleanRetryOf(overview?.work?.id)
      : '',
  }
}

const ACTION_OUTCOMES = new Set(['accepted', 'refreshed', 'blocked', 'unavailable'])

/**
 * Keep private contribution startup outcomes distinct. A replaced card is
 * ordinary reconciliation, an explicit 4xx is a real blocker, and only a
 * transport/service failure should be described as unavailable.
 */
export async function contributionActionOutcome(callback) {
  if (typeof callback !== 'function') return { kind: 'unavailable' }
  try {
    const value = await callback()
    if (value === true) return { kind: 'accepted' }
    if (value && ACTION_OUTCOMES.has(value.kind)) return value
    return { kind: 'unavailable' }
  } catch {
    return { kind: 'unavailable' }
  }
}

export function contributionStartFailureOutcome({
  status = 0, detail = '', actionChanged = false,
} = {}) {
  if (actionChanged) return { kind: 'refreshed' }
  const code = Number(status) || 0
  if (code >= 400 && code < 500) {
    return {
      kind: 'blocked',
      message: String(detail || '').trim()
        || 'This contribution action is not available in its current state.',
    }
  }
  return { kind: 'unavailable' }
}

export function chatContributionPrepareAction() {
  return {
    label: 'Prepare to submit',
    description: 'Align and review worthwhile work here, then bring back one exact public approval.',
  }
}

export function chatContributionFinishAction() {
  return {
    label: 'Prepare to submit',
    description: 'Resolve every private step without repeating work, then bring the exact send decision back here.',
  }
}

export function chatChangesPrimaryAction(overview) {
  if (overview?.workState === 'active') return null
  const counts = overview?.counts || {}
  if (counts.submitting > 0) return null
  const kinds = [
    counts.unsorted > 0 ? 'unsorted' : '',
    counts.prepared > 0 ? 'prepared' : '',
    counts.attention > 0 ? 'attention' : '',
  ].filter(Boolean)
  if (kinds.length > 1) return {
    kind: 'finish', label: 'Prepare to submit',
    description: 'Align new work, repair private reviews, and bring one exact send decision back here.',
  }
  if (counts.unsorted > 0) return {
    kind: 'prepare', label: 'Prepare to submit',
    description: 'Sort, align, and review the worthwhile work without leaving this chat.',
  }
  if (counts.attention > 0) return {
    kind: 'finish', label: 'Resolve all',
    description: 'Continue every private fix here and return only the decisions that still need you.',
  }
  if (counts.prepared > 0) return {
    kind: 'review', label: 'Review prepared',
    description: 'Review the exact private work, then send or update it directly when you approve.',
  }
  if (counts.open > 0) return {
    kind: 'updates', label: 'Check for updates',
    description: 'Refresh open pull requests and prepare any newer work that belongs with them.',
  }
  return null
}

export function preparedChangesPrimaryAction(values, { connected } = {}) {
  const items = (Array.isArray(values) ? values : []).map(value => (
    value?.kind ? value : { kind: 'record', id: value?.id, record: value }
  ))
  if (items.length === 0) return null
  if (items.some(item => item.kind === 'stack'
    ? item.records?.some(record => record?.status === 'submitting')
    : item.record?.status === 'submitting')) return null
  const ready = items.every(item => item.kind === 'stack'
    ? !stackSendBlocker(item, { connected })
    : !sendBlocker(item.record, { connected }))
  if (ready) {
    if (items.length === 1) {
      const item = items[0]
      const action = item.kind === 'stack'
        ? publicationStackAction(item)
        : publicationAction(item.record)
      return {
        kind: 'publish-items',
        items,
        label: action.label,
        description: item.kind === 'stack'
          ? 'Confirm the complete linked set once, then open every reviewed pull request in order.'
          : 'Complete this exact reviewed GitHub action.',
      }
    }
    const allUpdates = items.every(item => item.kind === 'stack'
      ? publicationStackAction(item).updating
      : item.record?.action === 'pr_update')
    const allStacks = items.every(item => item.kind === 'stack')
    return {
      kind: 'publish-items',
      items,
      label: allStacks && items.length === 2
        ? `${allUpdates ? 'Update' : 'Send'} both stacks`
        : `${allUpdates ? 'Update' : 'Send'} all ${items.length}${allStacks ? ' stacks' : ''}`,
      description: 'Confirm the complete reviewed units once, then publish every included action.',
    }
  }
  return {
    kind: 'fix-prepared',
    items,
    label: items.length === 1 ? 'Fix and review' : `Fix and review all ${items.length}`,
    description: 'Give every incomplete private review to the agent in one pass.',
  }
}
