/** Shared local date/time labels for the Möbius interface. */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function asDate(value) {
  if (value == null) return null
  const date = value instanceof Date ? value : new Date(value)
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

export function formatTime(value) {
  const date = asDate(value)
  if (!date) return ''
  return new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(date)
}

export function formatDateTime(value) {
  const date = asDate(value)
  if (!date) return ''
  const year = new Intl.DateTimeFormat('en-GB', { year: 'numeric' }).format(date)
  return `${ordinal(date.getDate())} ${MONTHS[date.getMonth()]} ${year}, ${formatTime(date)}`
}
