/** Shared local date/time labels for the Möbius interface. */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

const NAIVE_API_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/

/** Milliseconds for an API timestamp, or NaN. Möbius persists UTC as
 *  SQLite-compatible naive datetimes, so API timestamps can arrive without a
 *  zone suffix. Browsers read that shape as local wall time, which would put
 *  every label off by the viewer's UTC offset; an explicit zone is kept. */
export function parseApiTimestamp(value) {
  const raw = typeof value === 'string' ? value.trim() : ''
  if (!raw) return NaN
  return Date.parse(NAIVE_API_DATETIME.test(raw) ? `${raw}Z` : raw)
}

/** A Date for a Date, epoch milliseconds or an API timestamp; null otherwise. */
export function apiDate(value) {
  if (value == null) return null
  const date = value instanceof Date ? value
    : new Date(typeof value === 'string' ? parseApiTimestamp(value) : value)
  return Number.isNaN(date.getTime()) ? null : date
}

function ordinal(day) {
  const lastTwo = day % 100
  if (lastTwo >= 11 && lastTwo <= 13) return `${day}th`
  switch (day % 10) {
    case 1: return `${day}st`
    case 2: return `${day}nd`
    case 3: return `${day}rd`
    default: return `${day}th`
  }
}

// Constructing an Intl formatter costs far more than formatting with one, and
// transcripts label every row on each render, so each formatter is built once.
const TIME_FORMAT = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
})
const YEAR_FORMAT = new Intl.DateTimeFormat('en-GB', { year: 'numeric' })

export function formatTime(value) {
  const date = apiDate(value)
  if (!date) return ''
  return TIME_FORMAT.format(date)
}

const RELATIVE_FORMAT = new Intl.RelativeTimeFormat('en', { numeric: 'always' })

/** A recent moment as "Just now", "23 minutes ago" or "5 hours ago"; anything
 *  a day or more old, or further in the future than "Just now" covers, as its
 *  full date and time. A server clock a little ahead of this one stamps a
 *  just-finished reply up to a minute in the future: that is still now. */
export function formatRecentDateTime(value, now = Date.now()) {
  const date = apiDate(value)
  if (!date) return ''
  const elapsed = now - date.getTime()
  if (elapsed <= -60_000 || elapsed >= 86_400_000) return formatDateTime(date)
  if (elapsed < 60_000) return 'Just now'
  return elapsed < 3_600_000
    ? RELATIVE_FORMAT.format(-Math.floor(elapsed / 60_000), 'minute')
    : RELATIVE_FORMAT.format(-Math.floor(elapsed / 3_600_000), 'hour')
}

export function formatDateTime(value) {
  const date = apiDate(value)
  if (!date) return ''
  const year = YEAR_FORMAT.format(date)
  return `${ordinal(date.getDate())} ${MONTHS[date.getMonth()]} ${year}, ${formatTime(date)}`
}
