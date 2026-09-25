/* Whether a Subagents-app helper activity row still describes live work. */

// Statuses of a delegated helper that is still working. Its one activity row
// sits where it was launched and shows its final state once it settles.
const WORKING_STATUSES = new Set([
  'accepted', 'retrying', 'starting', 'running', 'resuming', 'paused',
])

export function isWorkingHelper(item) {
  return item?.type === 'helper_result' && WORKING_STATUSES.has(item.status)
}
