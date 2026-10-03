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

/** Only a delivered result can explain what started an answer. Undelivered
 * results and deliberate stops stay beside the answer that owned the wait. */
export function waitWokeItsAnswer(summary) {
  return summary?.status !== 'cancelled' && !summary?.delivery_pending
}

export function waitHistoryViewModel(summary) {
  const condition = waitConditionLabel(summary?.description)
  const settledOutcome = OUTCOMES[summary?.status]
  const outcome = summary?.delivery_pending && settledOutcome ? {
    ...settledOutcome,
    kicker: summary.status === 'met' ? 'Condition met · follow-up pending'
      : `${settledOutcome.kicker} · follow-up pending`,
    tone: 'attention',
    spoken: 'Follow-up pending',
  } : settledOutcome
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
    result: summary?.latest_result?.summary || null,
    ...outcome,
    ariaLabel: `${outcome.spoken}: ${condition}`,
    metadata: [owner, checks, duration].filter(Boolean).join(' · '),
  }
}
