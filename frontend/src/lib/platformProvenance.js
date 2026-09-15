export function formatUpstreamCommitDate(value, locale, timeZone) {
  if (typeof value !== 'string' || !value) return ''
  // A bare `YYYY-MM-DD` (the image's baked build date) carries no time, so
  // anchor it at noon UTC — rendering in any timezone then keeps the same
  // calendar day. A full ISO timestamp (a commit's `%cI`, which carries the
  // committer's offset) parses to its real instant. Both then render in the
  // same `timeZone` — the viewer's local zone when omitted — so "Installed
  // update" and "Current system" can't disagree by a day for the same commit.
  const bare = /^\d{4}-\d{2}-\d{2}$/.test(value)
  const date = new Date(bare ? `${value}T12:00:00Z` : value)
  if (Number.isNaN(date.getTime())) return ''
  try {
    return new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone,
    }).format(date)
  } catch {
    return value.slice(0, 10)
  }
}
