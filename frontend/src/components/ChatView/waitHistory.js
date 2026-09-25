/* Pure presentation helpers for settled Waiting transcript markers. */

import { waitConditionLabel } from './waitingPresentation.js'

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const remainder = total % 60
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const minuteRemainder = minutes % 60
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`
}

const OUTCOMES = {
  met: { kicker: 'Wait completed', tone: 'completed', spoken: 'Completed wait' },
  expired: { kicker: 'Wait reached its deadline', tone: 'attention', spoken: 'Expired wait' },
  failed: { kicker: 'Wait check failed', tone: 'attention', spoken: 'Failed wait' },
  cancelled: { kicker: 'Wait stopped', tone: 'stopped', spoken: 'Stopped wait' },
}

/**
 * Where a settled wait belongs within the answer the server anchored it to
 * (`terminal_wait_summaries_by_message_index` in backend/app/chat_waits.py).
 * A met/expired/failed wait CAUSED that answer — the wake turn starts because
 * of it — so it leads the answer and shows while that answer is still live,
 * exactly when the composer's Waiting chip disappears. A deliberate stop is an
 * outcome of the answer that owned the wait, so it trails it once settled.
 */
export function waitHistoryPlacement(summary) {
  return summary?.status === 'cancelled' ? 'trail' : 'lead'
}

export function waitHistoryViewModel(summary) {
  const condition = waitConditionLabel(summary?.description)
  const outcome = OUTCOMES[summary?.status]
  if (!condition || !outcome) return null
  const count = Number(summary?.checks_count)
  const checks = Number.isInteger(count) && count > 0
    ? `${count} ${count === 1 ? 'check' : 'checks'}`
    : null
  const duration = summary?.duration_seconds != null
    && Number.isFinite(Number(summary.duration_seconds))
    ? formatDuration(summary.duration_seconds)
    : null
  const owner = String(summary?.condition_owner || '').trim() || null
  return {
    condition,
    ...outcome,
    ariaLabel: `${outcome.spoken}: ${condition}`,
    metadata: [owner, checks, duration].filter(Boolean).join(' · '),
  }
}
