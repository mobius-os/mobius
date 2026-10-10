/** Pure projection of the durable container-rebuild job into Settings UI. */

export const ACTIVE_REBUILD_STATES = new Set([
  'queued', 'preparing', 'replacing', 'verifying',
])

export function rebuildIsActive(status) {
  return ACTIVE_REBUILD_STATES.has(status?.state)
}

// The self-hosted helper never claimed the queued request. It stays queued (so
// nothing starts beside it), and withdrawing it is safe: the helper renames the
// request out of the inbox before it acts.
export function rebuildAwaitingHostHelper(status) {
  return rebuildIsActive(status) && status?.code === 'host_helper_unclaimed'
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
      return 'The replacement needs recovery before another update can start. Check Recovery in your deployment.'
    default:
      return ''
  }
}

// How long the current stage has been running, from the controller's updated_at.
export function rebuildStartedAgo(status, now = Date.now()) {
  const started = Date.parse(status?.updated_at || '')
  if (!Number.isFinite(started)) return ''
  const minutes = Math.max(0, Math.floor((now - started) / 60000))
  if (minutes < 1) return 'just entered this stage'
  if (minutes < 60) return `in this stage for ${minutes} min`
  const hours = Math.floor(minutes / 60)
  return `in this stage for about ${hours} ${hours === 1 ? 'hour' : 'hours'}`
}

// Collapse a controller message to one short, safe status line.
function shortStatusText(message) {
  const text = String(message || '').replace(/\s+/g, ' ').trim()
  return text.length > 160 ? `${text.slice(0, 159)}…` : text
}

// One honest status line while a replacement is active: the controller's own
// stage text ("Selecting the verified Möbius image.", "Downloading and checking
// the official image.", "Railway is replacing the container.") when it sent one,
// else the fixed phase copy, plus how long the current stage has been running.
// An unclaimed host request keeps the fixed copy here; its actionable message is
// shown as the description.
export function rebuildStatusLine(status, now = Date.now()) {
  if (!rebuildIsActive(status)) return rebuildProgressMessage(status)
  const controller = rebuildAwaitingHostHelper(status) ? '' : shortStatusText(status?.message)
  const base = controller || rebuildProgressMessage(status)
  const elapsed = rebuildStartedAgo(status, now)
  return elapsed ? `${base} (${elapsed})` : base
}
