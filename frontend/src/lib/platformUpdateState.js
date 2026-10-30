/**
 * Pure state projection for the combined platform-update Settings row.
 *
 * Restart readiness and update availability are deliberately independent:
 * another reviewed release can be staged while one restart is already pending.
 */
import { rebuildIsActive } from './containerRebuild.js'

export function platformStatusFromApply(previous, result) {
  const state = result.state
  const upstream = result.upstream_commit || previous?.recorded_upstream_sha || null
  const clean = (
    state === 'restart_needed'
    || state === 'activation_needed'
    || state === 'up_to_date'
  )
  const failedOntoStagedUpdate = (
    (state === 'rolled_back' || state === 'conflict')
    && !!previous?.needs_restart
  )
  return {
    ...(previous || {}),
    state,
    status_unavailable: false,
    // A clean apply consumed the exact reviewed target. Do not briefly carry
    // the OLD `available:true` through the render before the status refresh:
    // the batched-update UI would otherwise offer the update that just applied.
    available: state === 'rolled_back',
    activation: result.activation || previous?.activation || null,
    needs_restart:
      state === 'restart_needed'
      || !!result.needs_restart
      || failedOntoStagedUpdate,
    current_build_sha: previous?.current_build_sha || null,
    recorded_upstream_sha: upstream,
    contained_upstream_sha: clean
      ? (result.upstream_commit || previous?.contained_upstream_sha || null)
      : (previous?.contained_upstream_sha || null),
    seed_required: false,
    conflict_paths: Array.isArray(result.conflict_paths)
      ? result.conflict_paths
      : [],
    conflict_chat_id: state === 'conflict' ? (result.chat_id || null) : null,
  }
}

export function platformStatusUnavailable(previous) {
  return {
    ...(previous || {}),
    state: 'unavailable',
    available: false,
    status_unavailable: true,
  }
}

export function platformUpdateStatusLabel(platform) {
  const state = platform?.state
  const needsRestart = !!platform?.needs_restart
  const available = !!platform?.available
  const activationLevel = platform?.activation?.level || (
    needsRestart ? 'server_restart' : 'live'
  )

  if (platform?.status_unavailable || state === 'unavailable') {
    return 'Update status unavailable'
  }
  if (state === 'conflict') return 'Update blocked'
  if (state === 'rolled_back') return 'Update needs repair'
  if (platform?.unfinished_update?.action === 'restart') return 'Restart to finish the update'
  if (requiresAgentActivation(platform?.activation)) return 'Update needs help'
  if (activationLevel !== 'live' && available) return 'More updates available'
  if (activationLevel === 'server_restart') return 'Ready to restart'
  if (activationLevel === 'proxy_reload') return 'Update needs help'
  if (activationLevel === 'container_recreate') return 'Update needs help'
  if (activationLevel === 'image_rebuild') return 'Ready to finish'
  if (activationLevel === 'host_maintenance') return 'Finish this update on your server'
  if (available && platform?.image_build_applied === false) return 'Code update required'
  if (available) return 'New update available'
  if (state === 'source_unconfirmed' || platform?.image_build_applied === false) return 'Running code not confirmed'
  return 'Up to date'
}

/**
 * The single frontend reading of the backend's deployment classifier.
 *
 * `platform_activation.deployment_kind` is binary — it returns exactly
 * 'railway' or 'self_hosted' — so the frontend must not invent a third kind.
 * `null` means "not read yet, or unreadable", which callers render as
 * guidance that covers both deployments rather than guessing one.
 */
export function deploymentKind(activation) {
  const deployment = activation?.deployment
  return deployment === 'railway' || deployment === 'self_hosted'
    ? deployment
    : null
}

export function deploymentKindLabel(activation) {
  return deploymentKind(activation) === 'railway' ? 'Railway' : 'Self-hosted'
}

/** External actions remain independent even when a release also needs an image. */
export function requiresAgentActivation(activation) {
  const routine = new Set(['server_restart', 'image_rebuild'])
  return activation?.required_actions?.some(action => !routine.has(action)) || false
}

/** Replacement handles image and in-container work, never external configuration. */
export function reviewedUpdateUsesContainerRebuild(preview) {
  const activation = preview?.activation
  return !!activation?.required_actions?.includes('image_rebuild') && !requiresAgentActivation(activation)
}

export function platformActivationLabel(activation) {
  const labels = {
    live: 'Live refresh',
    server_restart: 'Server restart',
    proxy_reload: 'Proxy reload',
    container_recreate: 'Container recreation',
    image_rebuild: 'Image rebuild',
    host_maintenance: 'Host maintenance',
  }
  return labels[activation?.level] || 'Activation details'
}

/** The activation level is independent of whether a newer release exists. */
export function platformActivationLevel(platform) {
  return platform?.activation?.level || (platform?.needs_restart ? 'server_restart' : 'live')
}

const STALL_REASONS = {
  resolver_idle: 'resolver idle',
  resolver_missing: 'no resolver chat',
}

/**
 * The stalled view of an unfinished update, or null. The backend decides
 * whether it stalled and which actions apply; this only words it. A refused
 * restart is not a pause while a container replacement is running: the new
 * image is what closes the package gap.
 */
export function platformUpdateStall(platform, rebuild) {
  const pending = platform?.unfinished_update
  const stalled = pending?.stalled
  if (!stalled) return null
  if (stalled.reason === 'restart_blocked' && rebuildIsActive(rebuild)) return null
  const actions = new Set(stalled.actions || [])
  const packages = (platform?.restart_blocker?.packages || []).map(item => item.name)
  const needs = packages.length ? ` (needs ${packages.join(', ')})` : ''
  const message = stalled.reason !== 'restart_blocked'
    ? `Update paused for ${Math.max(1, Math.round(stalled.idle_hours || 0))} h: ${STALL_REASONS[stalled.reason] || 'no progress'}.`
    : pending.action === 'replace'
      ? `Update paused: it needs a new container${needs}. Choose Finish update to replace this one.`
      : `Update paused: this container cannot restart into it${needs}.`
  return { message, canResume: actions.has('resume'), canRetry: actions.has('retry') }
}

function packageShortfall(item) {
  if (item.declared == null) return `${item.name} (a declaration Möbius cannot verify)`
  const declared = item.declared || 'any version'
  if (item.kind === 'unsupported') return `${item.name} (source declares ${declared}, which Möbius cannot verify)`
  return `${item.name} (source declares ${declared}, container ${item.installed ? `has ${item.installed}` : 'does not have it'})`
}

/**
 * The warning Settings shows while the served source declares Python
 * packages this container does not install as declared, or null. Boot keeps
 * serving such source (only the import check is the hard gate), so this is
 * the owner's signal that, for example, a security version bump is unmet.
 * A stall that already names the gap, or a container replacement already
 * under way, needs no second notice.
 */
export function platformPackageWarning(platform, rebuild) {
  const gap = platform?.restart_blocker
  const packages = gap?.packages || []
  if (!packages.length || rebuildIsActive(rebuild)) return null
  if (platform?.unfinished_update?.stalled?.reason === 'restart_blocked') return null
  const listed = packages.map(packageShortfall).join('; ')
  const running = gap.blocking
    ? 'Möbius keeps running, but cannot restart onto this source'
    : 'Möbius keeps running on the installed versions'
  const commit = (gap.fix?.commit || '').slice(0, 12)
  const revert = commit ? `revert local commit ${commit}` : 'revert the uncommitted edit'
  if (gap.origin === 'official_newer') {
    return {
      title: 'Container packages are behind the platform source',
      message: `${running} until the container is updated: ${listed}. A container update (Finish update) installs them.`
        + (packages.some(item => item.origin === 'local')
          ? ` Some come from a local change; if the new container still lacks them, ${revert}.`
          : ''),
    }
  }
  return {
    title: 'A local change needs Python packages this container lacks',
    message: `${running}: ${listed}. To fix it, ${revert}, or update the container once that change is released.`,
  }
}
