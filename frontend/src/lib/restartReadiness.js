export const RESTART_UNTRACKED_MIN_WAIT_MS = 7000
export const RESTART_POLL_FAST_ATTEMPTS = 40
export const RESTART_POLL_MAX_ATTEMPTS = 88
export const RESTART_POLL_FAST_INTERVAL_MS = 1500
export const RESTART_POLL_SLOW_INTERVAL_MS = 5000

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

/**
 * Poll quickly for the ordinary restart window, then keep checking gently for
 * another four minutes. A delayed container must self-heal instead of leaving
 * a permanent error at the old one-minute boundary.
 */
export function restartPollDecision(attempts = 0) {
  const count = Math.max(0, Number(attempts) || 0)
  return {
    slow: count >= RESTART_POLL_FAST_ATTEMPTS,
    timedOut: count >= RESTART_POLL_MAX_ATTEMPTS,
    delayMs: count >= RESTART_POLL_FAST_ATTEMPTS
      ? RESTART_POLL_SLOW_INTERVAL_MS
      : RESTART_POLL_FAST_INTERVAL_MS,
  }
}
