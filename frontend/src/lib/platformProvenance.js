export function formatUpstreamCommitDate(value, locale, timeZone) {
  if (typeof value !== 'string' || !value) return ''
  const bare = /^\d{4}-\d{2}-\d{2}$/.test(value)
  const date = new Date(bare ? `${value}T12:00:00Z` : value)
  if (Number.isNaN(date.getTime())) return ''
  // A bare `YYYY-MM-DD` (the image's baked build date) is a calendar day with no
  // instant, so always render it in UTC — it then reads the same day in every
  // timezone (a local render shifts it a day at UTC+13/+14). A full ISO timestamp
  // (a commit's `%cI`) is a real instant, shown in the caller's `timeZone` (the
  // viewer's local zone when omitted). With both rows carrying the same commit's
  // `%cI`, they still agree for that viewer.
  const zone = bare ? 'UTC' : timeZone
  try {
    return new Intl.DateTimeFormat(locale, {
      day: 'numeric',
      month: 'short',
      year: 'numeric',
      timeZone: zone,
    }).format(date)
  } catch {
    return value.slice(0, 10)
  }
}
