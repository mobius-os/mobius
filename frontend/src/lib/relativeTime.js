// Shared compact relative-time formatting for recency labels in the shell.
import { formatDateTime } from './dateTimeFormat.js'
// Coarse on purpose — a list hint, not a clock. Reused by the notification
// preview and global search so recency reads the same everywhere.

const NAIVE_API_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/
const PROJECT_DATE_FORMAT = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
const PROJECT_RELATIVE_FORMAT = new Intl.RelativeTimeFormat('en', { numeric: 'always' })

// Möbius persists UTC as SQLite-compatible naive datetimes, so API timestamps
// can arrive without a zone suffix. Browsers otherwise interpret that shape as
// local wall time, making a fresh update look hours old outside UTC.
export function parseApiTimestamp(value) {
  const raw = typeof value === 'string' ? value.trim() : ''
  if (!raw) return NaN
  return Date.parse(NAIVE_API_DATETIME.test(raw) ? `${raw}Z` : raw)
}

// Compact relative timestamp for a row. Falls back to a local date for
// anything older than a week or unparseable.
export function formatRelativeTime(isoString, now = Date.now()) {
  const t = parseApiTimestamp(isoString)
  if (!Number.isFinite(t)) return ''
  const diff = now - t
  if (diff < 60_000) return 'now'
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  try {
    return formatDateTime(new Date(t))
  } catch {
    return ''
  }
}

// Projects use a date-only label after the first week, rather than a timestamp.
export function formatProjectDate(isoString, now = Date.now()) {
  const timestamp = parseApiTimestamp(isoString)
  if (!Number.isFinite(timestamp)) return ''
  const elapsed = now - timestamp
  const distance = Math.abs(elapsed)
  if (distance >= 7 * 24 * 60 * 60 * 1000) return PROJECT_DATE_FORMAT.format(new Date(timestamp))
  if (distance < 60_000) return 'Just now'
  const [unit, length] = distance < 3_600_000 ? ['minute', 60_000]
    : distance < 86_400_000 ? ['hour', 3_600_000] : ['day', 86_400_000]
  return PROJECT_RELATIVE_FORMAT.format(-Math.sign(elapsed) * Math.floor(distance / length), unit)
}
