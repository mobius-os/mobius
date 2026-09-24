// A provider-limit park (design §2.4) carries `pause.resets_at` as an
// explicit-UTC ISO string. Render it as the viewer's LOCAL clock, by day: a park
// clamps up to 7 days out (chat.py _PARK_MAX_DELAY), so a bare time reads
// ambiguously across a day boundary ("Resets at 1:40 AM" — today or Tuesday?).
//
// The label carries its own preposition so a caller can splice it after
// "Resets" / "resets" and read naturally in every bucket. It always names the
// day so a bare clock time can never read ambiguously across a boundary — a park
// clamps up to 7 days out, so "at 1:40 AM" alone can't say today vs next week:
//   same-day  → "today at 13:40"
//   tomorrow  → "tomorrow at 13:40"
//   further   → "11th Sep 2026, 13:40"
// Returns null on a missing / unparseable value so the card degrades to just
// the message rather than showing a garbage label.
import { formatDateTime, formatTime } from '../../lib/dateTimeFormat.js'

export function formatResetTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const time = formatTime(d)
  // Compare local calendar days, not the raw 24h delta — a reset seven hours
  // from now can still be "tomorrow" if it crosses local midnight.
  const startOfDay = (x) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const dayDelta = Math.round(
    (startOfDay(d) - startOfDay(new Date())) / 86400000,
  )
  if (dayDelta <= 0) return `today at ${time}`
  if (dayDelta === 1) return `tomorrow at ${time}`
  return formatDateTime(d)
}
