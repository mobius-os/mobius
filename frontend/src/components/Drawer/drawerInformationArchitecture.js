function pinnedAt(item) {
  // Normalize to a bare UTC wall-clock string so values from different sources
  // compare consistently: optimistic client stamps are ISO-with-`Z`, while the
  // server serializes naive-UTC without a suffix. Both denote UTC, so dropping a
  // trailing `Z`/`+00:00` lets a plain lexicographic compare order them right
  // even when a refetch has replaced only one kind (chats OR apps).
  return (item?.pinned_at || '').replace(/(?:Z|\+00:00)$/, '')
}

// Oldest pin first, newest pin last. Pinning stamps pinned_at = now (the
// largest value), so a freshly pinned item lands at the BOTTOM of the pinned
// list, and drag-to-reorder re-stamps timestamps in this same ascending order.
function oldestPinnedFirst(a, b) {
  return pinnedAt(a.item).localeCompare(pinnedAt(b.item))
}

/**
 * Build the drawer's navigation projection without mutating query-cache arrays.
 *
 * Pinned chats and apps share one stable section ordered oldest-pin-first, so a
 * new pin appends at the bottom and manual drag-to-reorder owns the rest. The
 * ordinary chat history stays recency ordered; the apps grid keeps its
 * pinned-first ordering (also oldest-pin-first) then stable creation order.
 */
export function buildDrawerSections(chats = [], apps = []) {
  const chatRows = chats
    .filter(chat => chat.has_messages)
    .slice()
    .sort((a, b) => (
      ((b.activity_at || b.updated_at) || '')
        .localeCompare((a.activity_at || a.updated_at) || '')
    ))

  const appRows = apps
    .slice()
    .sort((a, b) => {
      const ap = pinnedAt(a)
      const bp = pinnedAt(b)
      if (ap && !bp) return -1
      if (!ap && bp) return 1
      if (ap && bp) return ap.localeCompare(bp)
      return (a.created_at || '').localeCompare(b.created_at || '')
    })

  const pinned = [
    ...chatRows
      .filter(chat => chat.pinned_at)
      .map(item => ({ kind: 'chat', item })),
    ...appRows
      .filter(app => app.pinned_at)
      .map(item => ({ kind: 'app', item })),
  ].sort(oldestPinnedFirst)

  return {
    pinned,
    chats: chatRows.filter(chat => !chat.pinned_at),
    apps: appRows,
  }
}

export function filterInstalledApps(apps = [], query = '') {
  const needle = String(query).trim().toLocaleLowerCase()
  if (!needle) return apps
  return apps.filter(app => (
    `${app.name || ''} ${app.description || ''} ${app.slug || ''}`
      .toLocaleLowerCase()
      .includes(needle)
  ))
}

export function appInitials(name) {
  const words = String(name || '')
    .replace(/[^a-z0-9]+/gi, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
  if (words.length === 0) return 'A'
  if (words.length === 1) return words[0].slice(0, 2).toLocaleUpperCase()
  return `${words[0][0]}${words[1][0]}`.toLocaleUpperCase()
}
