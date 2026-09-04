/* Recent selections are one bounded MRU list shared by shell navigation and search. */

export const RECENT_SELECTION_LIMIT = 12
const RECENT_SELECTIONS_STORAGE_KEY = 'mobius:global-search:recent-selections:v1'

function browserStorage() {
  try {
    return globalThis.localStorage || null
  } catch (_) {
    return null
  }
}

function normalizedRecentSelections(selections) {
  const seen = new Set()
  const normalized = []
  for (const selection of Array.isArray(selections) ? selections : []) {
    if (!['app', 'chat'].includes(selection?.kind)) continue
    const id = String(selection?.id ?? '').trim()
    if (!id) continue
    const key = `${selection.kind}:${id}`
    if (seen.has(key)) continue
    seen.add(key)
    normalized.push({ kind: selection.kind, id })
    if (normalized.length === RECENT_SELECTION_LIMIT) break
  }
  return normalized
}

// History stores only stable item references. Titles and other presentation
// data stay in their owning queries and are resolved afresh when search opens.
export function readRecentSelections(storage = browserStorage()) {
  try {
    return normalizedRecentSelections(JSON.parse(
      storage?.getItem(RECENT_SELECTIONS_STORAGE_KEY) || '[]',
    ))
  } catch (_) {
    return []
  }
}

export function rememberRecentSelection(selection, storage = browserStorage()) {
  const next = normalizedRecentSelections([
    selection,
    ...readRecentSelections(storage),
  ])
  try {
    storage?.setItem(RECENT_SELECTIONS_STORAGE_KEY, JSON.stringify(next))
  } catch (_) {
    // Restricted and private browsing contexts may reject Web Storage. Search
    // still works; it simply has no history on the next open.
  }
  return next
}

export function rememberRecentDestination(
  { view, chatId, appId },
  storage = browserStorage(),
) {
  if (view === 'chat' && chatId != null) {
    return rememberRecentSelection({ kind: 'chat', id: chatId }, storage)
  }
  if (view === 'canvas' && appId != null) {
    return rememberRecentSelection({ kind: 'app', id: appId }, storage)
  }
  return null
}

export function clearRecentSelections(storage = browserStorage()) {
  try {
    storage?.removeItem(RECENT_SELECTIONS_STORAGE_KEY)
  } catch (_) {
    // Clearing an unavailable store is already the desired end state.
  }
}

export function resolveRecentSelections(selections, chats, apps) {
  const chatsById = new Map(
    (Array.isArray(chats) ? chats : [])
      .filter(chat => chat?.id)
      .map(chat => [String(chat.id), chat]),
  )
  const appsById = new Map(
    (Array.isArray(apps) ? apps : [])
      .filter(app => app?.id)
      .map(app => [String(app.id), app]),
  )

  return normalizedRecentSelections(selections).flatMap(selection => {
    const value = selection.kind === 'chat'
      ? chatsById.get(selection.id)
      : appsById.get(selection.id)
    return value ? [{ kind: selection.kind, value }] : []
  })
}
