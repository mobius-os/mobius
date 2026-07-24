/**
 * Pure state projection for the combined platform-update Settings row.
 *
 * Restart readiness and update availability are deliberately independent:
 * another reviewed release can be staged while one restart is already pending.
 */

export function platformStatusFromApply(previous, result) {
  const state = result.state
  const upstream = result.upstream_commit || previous?.recorded_upstream_sha || null
  const clean = state === 'restart_needed' || state === 'up_to_date'
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

  if (state === 'conflict') return 'Update blocked'
  if (state === 'rolled_back') return 'Update needs repair'
  if (needsRestart && available) return 'More updates available'
  if (needsRestart) return 'Ready to restart'
  if (available) return 'New update available'
  return 'Up to date'
}
