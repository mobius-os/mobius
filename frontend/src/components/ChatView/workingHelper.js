/* Whether a helper row still describes live work, and who acts next for it. */

// Statuses of a delegated helper that is still working. Its one activity row
// sits where it was launched and shows its final state once it settles.
const WORKING_STATUSES = new Set([
  'accepted', 'retrying', 'starting', 'running', 'resuming', 'paused',
])

export function isWorkingHelper(item) {
  return item?.type === 'helper_result' && WORKING_STATUSES.has(item.status)
}

export function isWorkingHelperStatus(status) {
  return WORKING_STATUSES.has(status)
}

// `needs_input` means the helper asked its direct parent agent (ask_parent)
// and stopped. That agent answers or escalates; the owner acts only through
// a saved owner card, so this state must never read as a request to the owner.
export const HELPER_AWAITING_PARENT_LABEL = 'Waiting for coordinating agent'

export function isHelperAwaitingParent(item) {
  return item?.status === 'needs_input'
}

/** One vocabulary for delegated and provider-native helper rows and previews. */
export function helperPresentation(status) {
  if (status === 'paused') return { state: 'on_hold', label: 'On hold', dot: 'failed' }
  if (isWorkingHelperStatus(status)) return { state: 'working', label: 'Working', dot: 'running' }
  const settled = {
    completed: ['finished', 'Finished', 'done'],
    done: ['finished', 'Finished', 'done'],
    needs_input: ['asking', HELPER_AWAITING_PARENT_LABEL, 'running'],
    waiting: ['asking', HELPER_AWAITING_PARENT_LABEL, 'running'],
    failed: ['failed', 'Failed', 'failed'],
    needs_review: ['needs_review', 'Needs review', 'failed'],
    interrupted: ['interrupted', 'Interrupted', 'failed'],
  }
  const [state, label, dot] = settled[status] || ['stopped', 'Stopped', 'failed']
  return { state, label, dot }
}


// A paused or asking helper still owns unfinished work and may resume without
// its former footer row returning. Only terminal outcomes stop preview refresh.
const TERMINAL_HELPER_STATUSES = new Set([
  'completed', 'done', 'failed', 'needs_review', 'interrupted', 'stopped', 'cancelled', 'killed',
])

export function isTerminalHelperStatus(status) {
  return TERMINAL_HELPER_STATUSES.has(status)
}
