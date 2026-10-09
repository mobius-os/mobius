// App rows can show the app's own unread count as a pill. The app reports the
// count (PUT /api/apps/{id}/badge); the pill caps it so a large inbox never
// widens the row, and it supersedes the generic new-activity dot: a number
// already says that something is new, and how much.
export const APP_BADGE_CAP = 99

export function appBadgeLabel(count) {
  const value = Number(count)
  if (!Number.isFinite(value) || value < 1) return null
  return value > APP_BADGE_CAP ? `${APP_BADGE_CAP}+` : String(Math.floor(value))
}

// What a drawer row shows for unread state: the pill label (app rows only) and
// whether the quieter activity dot still applies.
export function drawerRowUnread(kind, item, attention) {
  const badgeLabel = kind === 'app' ? appBadgeLabel(item?.badge_count) : null
  return { badgeLabel, attentionDot: !!attention && !badgeLabel }
}
