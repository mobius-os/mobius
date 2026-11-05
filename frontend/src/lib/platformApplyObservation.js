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

const UNCONFIRMED_MESSAGE = 'The update could not be confirmed. Check the current status and review again before retrying.'

/**
 * Wait until this plan's update ends. ``after`` is the progress timestamp read
 * before Apply was sent: only a record newer than it can be this attempt, so an
 * earlier attempt of the same plan is never mistaken for it. ``null`` means
 * that read failed. The first record read afterwards then stands in for it,
 * unless that record is this plan's update still running, so a finished record
 * that may predate the request is never accepted as its outcome.
 *
 * Resolves ``{ settled: true, progress }`` with the terminal record, or
 * ``{ settled: false, reason }`` with ``not_started``, ``unreachable``, or
 * ``superseded`` (another plan's record replaced the update being followed).
 */
export async function waitForApplyOutcome({ planId, after = null, readProgress, sleep, now = Date.now }) {
  const started = now()
  let reachedAt = started
  let seen = false
  let baseline = after
  for (;;) {
    const progress = await readProgress().catch(() => null)
    if (progress) reachedAt = now()
    const mine = progress?.plan_id === planId
    if (seen && progress && !mine) return { settled: false, reason: 'superseded' }
    if (baseline === null && progress) baseline = mine && progress.active ? 0 : progress.updated_at
    const current = mine && progress.updated_at > baseline
    if (current && !progress.active) return { settled: true, progress }
    seen = seen || current
    if (!seen && progress && now() - started >= APPLY_ADMISSION_GRACE_MS) {
      return { settled: false, reason: 'not_started' }
    }
    if (now() - reachedAt >= APPLY_UNREACHABLE_LIMIT_MS) return { settled: false, reason: 'unreachable' }
    await sleep(APPLY_OBSERVE_INTERVAL_MS)
  }
}

/**
 * What the review and Settings show once an observed Apply has ended and the
 * status read that follows it has been tried (``status`` is null when it
 * failed). A completed update whose status cannot be read still closes the
 * review, but says so: its activation state is unknown until a read succeeds.
 */
export function observedApplyResult(outcome, status) {
  if (!outcome.settled) {
    return { ok: false, error: UNCONFIRMED_MESSAGE, errorCode: 'update_plan_stale', notice: '' }
  }
  const { phase, error } = outcome.progress
  const complete = phase === 'complete'
  return {
    ok: complete,
    state: status?.state,
    error: error || (phase === 'failed' ? 'The update could not finish. Review its current status.' : ''),
    errorCode: '',
    notice: complete && !status ? 'The update finished, but its new status could not be read. Check for updates to see what it needs next.' : '',
  }
}
