/** Pure projection of the durable container-rebuild job into Settings UI. */

export const ACTIVE_REBUILD_STATES = new Set([
  'queued', 'preparing', 'waiting_for_work', 'replacing', 'verifying',
])

export function rebuildIsActive(status) {
  return ACTIVE_REBUILD_STATES.has(status?.state)
}

export function rebuildPollShouldContinue(status) {
  return status === null || rebuildIsActive(status)
}

export function rebuildProgressMessage(status) {
  switch (status?.state) {
    case 'queued':
    case 'preparing':
      return 'Preparing the new container…'
    case 'waiting_for_work':
      return 'Waiting for active work to finish…'
    case 'replacing':
      return 'Replacing the container…'
    case 'verifying':
      return 'Checking that Möbius came back…'
    case 'succeeded':
      return 'Container rebuilt successfully.'
    case 'no_change':
      return 'This container is already current.'
    case 'rolled_back':
      return 'The rebuild failed, so the previous container was restored.'
    case 'needs_recovery':
      return 'The container could not be restored. Use your deployment’s Recovery action.'
    default:
      return ''
  }
}
