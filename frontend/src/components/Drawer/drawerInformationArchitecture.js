function pinnedAt(item) {
  return item?.pinned_at || ''
}

function newestPinnedFirst(a, b) {
  return pinnedAt(b.item).localeCompare(pinnedAt(a.item))
}

/**
 * Build the drawer's navigation projection without mutating query-cache arrays.
 *
 * Pinned chats and apps share one stable section, while the ordinary chat
 * history remains recency ordered. Apps preserve the drawer's familiar
 * pinned-first ordering, then the stable creation order within the rest.
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
      if (ap && bp) return bp.localeCompare(ap)
      return (a.created_at || '').localeCompare(b.created_at || '')
    })

  const pinned = [
    ...chatRows
      .filter(chat => chat.pinned_at)
      .map(item => ({ kind: 'chat', item })),
    ...appRows
      .filter(app => app.pinned_at)
      .map(item => ({ kind: 'app', item })),
  ].sort(newestPinnedFirst)

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
