/** Pure projection of the durable container-rebuild job into Settings UI. */

export const ACTIVE_REBUILD_STATES = new Set([
  'queued', 'preparing', 'replacing', 'verifying',
])

export function rebuildIsActive(status) {
  return ACTIVE_REBUILD_STATES.has(status?.state)
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
  switch (status?.state) {
    case 'queued':
    case 'preparing':
      return 'Preparing the system update…'
    case 'replacing':
      return 'Installing the system update…'
    case 'verifying':
      return 'Checking that Möbius came back…'
    case 'succeeded':
      return 'The updated system is ready.'
    case 'no_change':
      return status?.release_source === 'latest_ghcr'
        ? 'Möbius already uses the latest official version.'
        : 'Möbius already uses the installed update.'
    case 'rolled_back':
      return 'The update did not start correctly, so Möbius restored its previous system image.'
    case 'needs_recovery':
      return 'Möbius could not return to the previous version. Use Recovery in your deployment.'
    default:
      return ''
  }
}
