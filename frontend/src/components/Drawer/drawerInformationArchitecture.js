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

function recentAt({ kind, item }) {
  if (kind === 'chat') {
    return item?.activity_at || item?.updated_at || item?.created_at || ''
  }
  return item?.last_opened_at || item?.updated_at || item?.created_at || ''
}

function newestRecentFirst(a, b) {
  return recentAt(b).localeCompare(recentAt(a))
}

/**
 * Build the drawer's navigation projection without mutating query-cache arrays.
 *
 * Pinned chats and apps share one stable section ordered oldest-pin-first, so a
 * new pin appends at the bottom and manual drag-to-reorder owns the rest.
 * Unpinned chats and apps share one newest-first Recents section. Chat activity
 * follows owner conversation activity; app activity follows explicit opens,
 * falling back to the bundle update/creation time until the first open. The
 * searchable apps grid keeps its pinned-first then stable creation ordering.
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

  const recents = [
    ...chatRows
      .filter(chat => !chat.pinned_at)
      .map(item => ({ kind: 'chat', item })),
    ...appRows
      .filter(app => !app.pinned_at)
      .map(item => ({ kind: 'app', item })),
  ].sort(newestRecentFirst)

  return {
    pinned,
    recents,
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

// The shared menu names a row by identity, so it must survive that row
// disappearing mid-render (deleted, filtered, or refreshed away) as ordinary
// absence rather than a crash.
export function findDrawerMenuItem(menu, chats = [], apps = []) {
  if (!menu) return null
  const items = menu.kind === 'chat' ? (chats || []) : (apps || [])
  return items.find(item => item?.id === menu.id) || null
}
