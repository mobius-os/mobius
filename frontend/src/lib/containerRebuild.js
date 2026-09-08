/** Pure projection of the durable container-rebuild job into Settings UI. */

export const ACTIVE_REBUILD_STATES = new Set([
  'queued', 'preparing', 'replacing', 'verifying',
])

export function rebuildIsActive(status) {
  return ACTIVE_REBUILD_STATES.has(status?.state)
}

export function rebuildNeedsBootstrap(status) {
  return status?.bootstrap_available === true
}

export function rebuildPollShouldContinue(status) {
  return status === null || rebuildIsActive(status)
}

export function rebuildRequestOutcome(status, { reviewedUpdate = false } = {}) {
  const state = typeof status?.state === 'string' ? status.state : ''
  const cutoverAccepted = rebuildIsActive(status) || state === 'succeeded'
  const alreadyCurrent = reviewedUpdate && state === 'no_change'
  return {
    state,
    accepted: cutoverAccepted || alreadyCurrent,
    cutoverAccepted,
    alreadyCurrent,
    terminalFailure: ['failed', 'rolled_back', 'needs_recovery'].includes(state),
  }
}

export function rebuildProgressMessage(status) {
  if (rebuildNeedsBootstrap(status)) {
    switch (status?.state) {
      case 'queued':
      case 'preparing':
        return 'Preparing the one-time container upgrade…'
      case 'replacing':
        return 'Enabling safe container updates…'
      case 'verifying':
        return 'Checking the upgraded container…'
      case 'succeeded':
        return 'Container updates are now enabled.'
      case 'rolled_back':
        return 'The upgrade was unhealthy, so the previous container was restored.'
      case 'needs_recovery':
        return 'The previous container could not be restored. Use your deployment’s Recovery action.'
      default:
        break
    }
  }
  switch (status?.state) {
    case 'queued':
    case 'preparing':
      return 'Preparing the new container…'
    case 'replacing':
      return 'Rebuilding the container…'
    case 'verifying':
      return 'Checking that Möbius came back…'
    case 'succeeded':
      return 'Container rebuilt successfully.'
    case 'no_change':
      return status?.release_source === 'latest_ghcr'
        ? 'This container already matches the latest official image.'
        : 'This container already matches the applied Möbius version.'
    case 'rolled_back':
      return 'The rebuild failed, so the previous container was restored.'
    case 'needs_recovery':
      return 'The container could not be restored. Use your deployment’s Recovery action.'
    default:
      return ''
  }
}
