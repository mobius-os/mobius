function apiDate(value) {
  if (!value) return null
  const text = String(value)
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(text) ? text : `${text}Z`)
  return Number.isNaN(date.getTime()) ? null : date
}

function clockLabel(value) {
  return apiDate(value)?.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  }) || null
}

function dateTimeLabel(value) {
  return apiDate(value)?.toLocaleString([], {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  }) || 'not set'
}

function cadenceLabel(wait) {
  if (wait.kind === 'timer') return 'one-time timer'
  const seconds = Number(wait.interval_secs) || 300
  if (seconds < 120) return 'every minute'
  if (seconds < 3600) return `every ${Math.round(seconds / 60)} minutes`
  const hours = Math.round(seconds / 3600)
  return `every ${hours} ${hours === 1 ? 'hour' : 'hours'}`
}

export function resourcePausePresentation(block, summary) {
  const kind = block?.pause?.kind
  const next = clockLabel(block?.pause?.resets_at)
  const storage = kind === 'storage'
  return {
    summary,
    next: next ? `checks again ${next}` : 'checks again automatically',
    owner: 'Möbius resource monitor',
    pressure: storage
      ? 'Storage reached its measured critical boundary'
      : 'Memory reached its measured critical boundary',
    wakeUp: 'This chat resumes automatically when pressure clears',
    usage: 'No model tokens while waiting · one turn when it resumes',
  }
}

export function waitPresentation(wait) {
  const cadence = cadenceLabel(wait)
  const next = clockLabel(wait.next_check_at)
  const due = clockLabel(wait.due_at)
  const count = Number(wait.checks_count) || 0
  const last = clockLabel(wait.last_checked_at)
  const activity = count
    ? `${count} ${count === 1 ? 'check' : 'checks'}${last ? ` · last at ${last}` : ''}`
    : 'Not checked yet'
  const summary = wait.kind === 'timer'
    ? (due ? `resumes ${due}` : 'resumes later')
    : (next ? `next check ${next}` : cadence)

  return {
    condition: String(wait.description || 'External condition'),
    owner: wait.condition_owner || (wait.kind === 'timer' ? 'Time' : 'External system'),
    summary,
    checker: `Möbius · ${cadence}${wait.kind !== 'timer' && next ? ` · next at ${next}` : ''}`,
    activity,
    timeout: `This chat wakes to investigate at ${dateTimeLabel(wait.deadline_at)}`,
    usage: 'No model tokens while checking · one turn when it wakes',
  }
}
