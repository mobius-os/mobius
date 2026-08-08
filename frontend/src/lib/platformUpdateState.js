/**
 * Pure state projection for the combined platform-update Settings row.
 *
 * Restart readiness and update availability are deliberately independent:
 * another reviewed release can be staged while one restart is already pending.
 */

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

export function platformUpdateStatusLabel(platform) {
  const state = platform?.state
  const needsRestart = !!platform?.needs_restart
  const available = !!platform?.available
  const activationLevel = platform?.activation?.level || (
    needsRestart ? 'server_restart' : 'live'
  )

  if (state === 'conflict') return 'Update blocked'
  if (state === 'rolled_back') return 'Update needs repair'
  if (activationLevel !== 'live' && available) return 'More updates available'
  if (
    activationLevel === 'server_restart'
    || activationLevel === 'dependency_sync'
  ) return 'Ready to restart'
  if (activationLevel === 'proxy_reload') return 'Proxy reload required'
  if (activationLevel === 'container_recreate') return 'Deployment required'
  if (activationLevel === 'image_rebuild') return 'Image rebuild required'
  if (activationLevel === 'host_maintenance') return 'Host maintenance required'
  if (available) return 'New update available'
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

export function platformActivationLabel(activation) {
  const labels = {
    live: 'Live refresh',
    server_restart: 'Server restart',
    dependency_sync: 'Dependency update',
    proxy_reload: 'Proxy reload',
    container_recreate: 'Container recreation',
    image_rebuild: 'Image rebuild',
    host_maintenance: 'Host maintenance',
  }
  return labels[activation?.level] || 'Activation details'
}
