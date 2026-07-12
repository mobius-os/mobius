// A provider-limit park (design §2.4) carries `pause.resets_at` as an
// explicit-UTC ISO string. Render it as the viewer's LOCAL clock, by day: a park
// clamps up to 7 days out (chat.py _PARK_MAX_DELAY), so a bare time reads
// ambiguously across a day boundary ("Resets at 1:40 AM" — today or Tuesday?).
//
// The label carries its own preposition so a caller can splice it after
// "Resets" / "resets" and read naturally in every bucket: same-day → "at
// 1:40 AM", tomorrow → "tomorrow at 1:40 AM", further out → "Tue at 1:40 AM".
// Returns null on a missing / unparseable value so the card degrades to just
// the message rather than showing a garbage label.
export function formatResetTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  // Compare local calendar days, not the raw 24h delta — a reset seven hours
  // from now can still be "tomorrow" if it crosses local midnight.
  const startOfDay = (x) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const dayDelta = Math.round(
    (startOfDay(d) - startOfDay(new Date())) / 86400000,
  )
  if (dayDelta <= 0) return `at ${time}`
  if (dayDelta === 1) return `tomorrow at ${time}`
  return `${d.toLocaleDateString([], { weekday: 'short' })} at ${time}`
}
