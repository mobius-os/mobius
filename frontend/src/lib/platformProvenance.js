import { formatRelativeTime } from './relativeTime.js'

export function formatUpstreamCommitDate(value, locale) {
  const match = typeof value === 'string'
    ? value.match(/^(\d{4})-(\d{2})-(\d{2})/)
    : null
  if (!match) return ''
  const [, year, month, day] = match
  const date = new Date(`${year}-${month}-${day}T12:00:00Z`)
  try {
    return new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(date)
  } catch {
    return `${year}-${month}-${day}`
  }
}

export function formatUpstreamCheckTime(value, now = Date.now()) {
  const relative = formatRelativeTime(value, now)
  return relative ? `Last checked ${relative}` : ''
}
