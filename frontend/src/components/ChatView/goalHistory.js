/* Pure presentation helpers for terminal Goal transcript summaries. */

export function formatGoalDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const remainder = total % 60
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const minuteRemainder = minutes % 60
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`
}

export function goalHistoryViewModel(summary) {
  const objective = String(summary?.objective || '').trim()
  if (!objective || !['completed', 'failed'].includes(summary?.status)) return null
  const completed = summary.status === 'completed'
  const done = summary?.plan?.summary?.completed
  const total = summary?.plan?.summary?.total
  const progress = Number.isInteger(done) && Number.isInteger(total)
    ? `${done} of ${total} steps complete`
    : null
  const duration = summary.duration_seconds != null
    && Number.isFinite(Number(summary.duration_seconds))
    ? formatGoalDuration(summary.duration_seconds)
    : null
  return {
    objective,
    completed,
    kicker: completed ? 'Goal completed' : 'Goal needs attention',
    ariaLabel: `${completed ? 'Completed goal' : 'Goal needing attention'}: ${objective}`,
    metadata: [progress, duration].filter(Boolean).join(' · '),
    hasPlan: Array.isArray(summary?.plan?.tasks) && summary.plan.tasks.length > 0,
  }
}
