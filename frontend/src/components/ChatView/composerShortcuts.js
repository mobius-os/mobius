export function resolveComposerEnterAction(event, {
  hasInput = false,
  canSteer = false,
  canRequestSteer = canSteer,
  canSubmitSteer = canRequestSteer,
  isTouchPrimary = false,
} = {}) {
  if (!event || event.key !== 'Enter' || event.shiftKey) return null

  const modifiedEnter = !!(event.metaKey || event.ctrlKey)
  if (!modifiedEnter && isTouchPrimary) return null

  if (hasInput) {
    if (modifiedEnter && canSubmitSteer) return 'submit-steer'
    return 'submit'
  }
  if (canRequestSteer) return 'steer'
  return 'noop'
}

export const DOUBLE_ESCAPE_STOP_WINDOW_MS = 700

export function resolveDoubleEscapeStop(event, {
  lastEscapeAt = 0,
  now = 0,
} = {}) {
  if (
    !event
    || event.defaultPrevented
    || event.key !== 'Escape'
    || event.repeat
    || event.altKey
    || event.ctrlKey
    || event.metaKey
    || event.shiftKey
  ) return { stop: false, lastEscapeAt }

  const stop = lastEscapeAt > 0
    && now - lastEscapeAt <= DOUBLE_ESCAPE_STOP_WINDOW_MS
  return { stop, lastEscapeAt: stop ? 0 : now }
}
