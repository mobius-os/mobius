export const RESTART_UNTRACKED_MIN_WAIT_MS = 7000

export function restartCanReload({
  previousBootId = '',
  currentBootId = '',
  sawUnavailable = false,
  elapsedMs = 0,
} = {}) {
  if (previousBootId && currentBootId) {
    return currentBootId !== previousBootId
  }
  return sawUnavailable || elapsedMs >= RESTART_UNTRACKED_MIN_WAIT_MS
}
