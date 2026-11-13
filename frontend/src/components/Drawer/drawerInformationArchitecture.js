// Normalize to a bare UTC wall-clock string so values from different sources
// compare consistently: optimistic client stamps are ISO-with-`Z`, while the
// server serializes naive-UTC without a suffix. Both denote UTC, so dropping a
// trailing `Z`/`+00:00` lets a plain lexicographic compare order them right
// even when a refetch has replaced only one kind (chats OR apps).
function normalizedStamp(value) {
  return (value || '').replace(/(?:Z|\+00:00)$/, '')
}

function pinnedAt(item) {
  return normalizedStamp(item?.pinned_at)
}

function pinnedAtMillis(item) {
  const value = item?.pinned_at
  if (!value) return Number.NaN
  const normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`
  return Date.parse(normalized)
}

/**
 * Give an in-flight pin a temporary rank that is guaranteed to append.
 *
 * The server remains authoritative after the write, but using Date.now()
 * alone makes a slow or incorrectly-set device clock place a new pin above an
 * existing one until the refetch finishes. Include every currently visible
 * rank and the last optimistic rank so rapid pins are monotonic too.
 */
export function nextPendingPinnedAt(
  chats = [],
  apps = [],
  projects = [],
  { now = Date.now(), previous = null } = {},
) {
  let next = Number.isFinite(Number(now)) ? Number(now) : Date.now()
  for (const item of [...(chats || []), ...(apps || []), ...(projects || [])]) {
    const millis = pinnedAtMillis(item)
    if (Number.isFinite(millis)) next = Math.max(next, millis + 1)
  }
  const previousMillis = pinnedAtMillis({ pinned_at: previous })
  if (Number.isFinite(previousMillis)) next = Math.max(next, previousMillis + 1)
  return new Date(next).toISOString()
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
  if (kind === 'artifact') return item?.last_opened_at || ''
  // A project the agent created but the owner never opened is still new to
  // them: rank it by creation, never by file edits (updated_at), so work in
  // its tree cannot manufacture recency.
  if (kind === 'project') return item?.last_opened_at || item?.created_at || ''
  return item?.last_opened_at || item?.updated_at || item?.created_at || ''
}

function newestRecentFirst(a, b) {
  return normalizedStamp(recentAt(b)).localeCompare(normalizedStamp(recentAt(a)))
}

/**
 * Keep an owner's pin/unpin choice visible while the server write settles.
 *
 * The query cache remains server-owned. A focus/reconnect read that began
 * before the mutation may still briefly publish its older snapshot, so the
 * drawer projects the pending choice over whichever list revision is current.
 * This is one shared boundary for chats, apps, and projects rather than three
 * ad-hoc optimistic caches that can each be overwritten independently.
 */
export function projectPendingDrawerPins(
  chats = [],
  apps = [],
  projects = [],
  pendingPins = new Map(),
) {
  const project = (kind, items) => (items || []).map(item => {
    const pending = pendingPins.get(`${kind}:${item.id}`)
    if (!pending) return item
    return {
      ...item,
      pinned_at: pending.pinned ? pending.pinnedAt : null,
    }
  })

  return {
    chats: project('chat', chats),
    apps: project('app', apps),
    projects: project('project', projects),
  }
}

/**
 * Build the drawer's navigation projection without mutating query-cache arrays.
 *
 * Pinned chats and apps share one stable section ordered
 * oldest-pin-first, so a
 * new pin appends at the bottom and manual drag-to-reorder owns the rest.
 * Unpinned chats outside any project share one newest-first Recents section
 * (project chats are reached from their project) and every installed
 * app appears in its own newest-first Apps strip; archived
 * chats leave both for the Archived section, newest archive first. Projects
 * never appear in the drawer: a project pin only lifts it to the top of the
 * Projects screen. Their built artifacts list inside the project folder.
 * Chat activity follows owner conversation activity; app activity follows
 * explicit opens, falling back to bundle update/creation time until the first
 * open. The searchable apps grid keeps its own stable ordering.
 */
export function buildDrawerSections(chats = [], apps = [], projects = []) {
  const conversationRows = chats.filter(chat => chat.has_messages)
  // Archived chats leave Pinned and Recents for their own section, most
  // recently archived first. The server clears a pin when it archives.
  const archived = conversationRows
    .filter(chat => chat.archived_at)
    .sort((a, b) => (
      normalizedStamp(b.archived_at).localeCompare(normalizedStamp(a.archived_at))
    ))
    .map(item => ({ kind: 'chat', item }))
  const chatRows = conversationRows
    .filter(chat => !chat.archived_at)
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

  // A chat linked to a project lives inside that project's workspace, so it
  // stays out of Recents. An explicit pin still lifts it into Pinned.
  const recents = [
    ...chatRows
      .filter(chat => !chat.pinned_at && !chat.project)
      .map(item => ({ kind: 'chat', item })),
  ].sort(newestRecentFirst)

  // Every installed app, pinned or not, lives in the drawer's Apps strip rather
  // than Recents, most recently opened first. Pinning adds a Pinned shortcut;
  // it never removes the app from the strip.
  const recentApps = appRows
    .map(item => ({ kind: 'app', item }))
    .sort(newestRecentFirst)
    .map(({ item }) => item)

  return {
    pinned,
    recents,
    recentApps,
    archived,
    apps: appRows,
  }
}

/**
 * The row's own stable order. Newly installed apps join at the front and
 * removed ones drop out. When an app becomes the most recently opened
 * (`promote`), it moves to the front only if it sat beyond the icons visible
 * without scrolling (`visibleCount`); an app already in view keeps its place,
 * so opening it never reshuffles what the owner is looking at.
 */
export function reconcileStripOrder(previous, recentIds, promote, visibleCount) {
  const ids = recentIds.map(String)
  // Lists still loading: keep the remembered order untouched.
  if (!ids.length) return (previous || []).map(String)
  const known = new Set(ids)
  const kept = (previous || []).map(String).filter(id => known.has(id))
  const keptSet = new Set(kept)
  const next = [...ids.filter(id => !keptSet.has(id)), ...kept]
  if (promote != null) {
    const at = next.indexOf(String(promote))
    if (at >= visibleCount) {
      next.splice(at, 1)
      next.unshift(String(promote))
    }
  }
  return next
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
export function findDrawerMenuItem(menu, chats = [], apps = [], projects = []) {
  if (!menu) return null
  const items = menu.kind === 'chat'
    ? (chats || [])
    : menu.kind === 'project'
      ? (projects || [])
      : (apps || [])
  return items.find(item => item?.id === menu.id) || null
}
