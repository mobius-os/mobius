export function resolveComposerEnterAction(event, {
  hasInput = false,
  canSteer = false,
  canRequestSteer = canSteer,
} = {}) {
  if (!event || event.key !== 'Enter' || event.shiftKey) return null

  if (hasInput) return 'submit'
  if (canRequestSteer) return 'steer'
  return 'noop'
}
