/**
 * Follow an admitted Apply through the server's durable progress record.
 *
 * The server finishes an admitted update even after its HTTP client leaves, so
 * when the Apply reply is lost the progress record — not the reply — owns the
 * outcome. Observing only reads: a lost reply never becomes a second Apply.
 */

export const APPLY_OBSERVE_INTERVAL_MS = 1500
// A request still in flight is admitted within seconds; an update that has not
// appeared after this long was not started.
export const APPLY_ADMISSION_GRACE_MS = 60_000
// The server stayed unreachable this long: stop guessing and show status.
export const APPLY_UNREACHABLE_LIMIT_MS = 5 * 60_000

/**
 * Wait until this plan's update ends. ``after`` is the progress timestamp read
 * before Apply was sent, so an earlier attempt of the same plan cannot be
 * mistaken for this one.
 *
 * Resolves ``{ settled: true, progress }`` with the terminal record, or
 * ``{ settled: false, reason }`` with ``not_started`` or ``unreachable``.
 */
export async function waitForApplyOutcome({ planId, after = 0, readProgress, sleep, now = Date.now }) {
  const started = now()
  let reachedAt = started
  let seen = false
  for (;;) {
    const progress = await readProgress().catch(() => null)
    if (progress) reachedAt = now()
    const current = progress?.plan_id === planId && progress.updated_at > after
    if (current && !progress.active) return { settled: true, progress }
    seen = seen || current
    if (!seen && progress && now() - started >= APPLY_ADMISSION_GRACE_MS) {
      return { settled: false, reason: 'not_started' }
    }
    if (now() - reachedAt >= APPLY_UNREACHABLE_LIMIT_MS) return { settled: false, reason: 'unreachable' }
    await sleep(APPLY_OBSERVE_INTERVAL_MS)
  }
}
