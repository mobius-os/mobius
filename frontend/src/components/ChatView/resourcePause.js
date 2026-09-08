const RESOURCE_PAUSE_KINDS = new Set(['memory', 'storage'])

export function isResourcePause(block) {
  return RESOURCE_PAUSE_KINDS.has(block?.pause?.kind)
}

export function resourcePauseLabel(block) {
  if (block?.pause?.kind === 'memory') return 'Waiting for memory headroom'
  if (block?.pause?.kind === 'storage') return 'Waiting for storage headroom'
  return null
}
