// Shared compact relative-time formatting for recency labels in the shell.
// Coarse on purpose — a list hint, not a clock. Reused by the notification
// preview and global search so recency reads the same everywhere.

// Compact relative timestamp for a row. Falls back to a local date for
// anything older than a week or unparseable.
export function formatRelativeTime(isoString, now = Date.now()) {
  const t = Date.parse(isoString)
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
    return new Date(t).toLocaleDateString()
  } catch {
    return ''
  }
}
