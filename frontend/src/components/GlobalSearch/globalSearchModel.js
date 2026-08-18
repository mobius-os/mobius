/* Pure ranking for installed-app search across names and manifest-derived metadata. */

function normalize(value) {
  return String(value ?? '')
    .normalize('NFKD')
    .replace(/\p{M}/gu, '')
    .toLocaleLowerCase()
}

function queryTokens(query) {
  return normalize(query).match(/[\p{L}\p{N}_-]+/gu) || []
}

function declaredManifestText(value, key = '') {
  if (value == null || value === false) return ''
  if (value === true) return key
  if (typeof value === 'string') {
    const clean = value.trim()
    if (!clean || clean.toLowerCase() === 'none') return ''
    return `${key} ${clean}`
  }
  if (typeof value === 'number') return `${key} ${value}`
  if (Array.isArray(value)) {
    const children = value.map(item => declaredManifestText(item)).filter(Boolean)
    return children.length ? `${key} ${children.join(' ')}` : ''
  }
  if (typeof value !== 'object') return ''
  return Object.entries(value)
    .map(([childKey, childValue]) => declaredManifestText(childValue, childKey))
    .filter(Boolean)
    .join(' ')
}

export function appManifestSearchDocument(app) {
  return normalize([
    app?.slug,
    app?.version,
    app?.system_prompt_file,
    app?.offline_capable ? 'offline offline_capable' : '',
    app?.embeds_agent ? 'agent embeds_agent' : '',
    app?.manage_apps ? 'manage_apps' : '',
    app?.manage_skills ? 'manage_skills' : '',
    app?.github_access ? 'github github_access' : '',
    app?.github_connect ? 'github github_connect' : '',
    app?.filesystem_access ? 'filesystem filesystem_access' : '',
    declaredManifestText(app?.capability_contract),
    declaredManifestText(app?.offline_contract),
  ].filter(Boolean).join(' '))
}

function includesEvery(text, tokens) {
  return tokens.every(token => text.includes(token))
}

export function searchInstalledApps(apps, query, limit = 8) {
  const tokens = queryTokens(query)
  if (!tokens.length) return []

  const ranked = []
  for (const app of Array.isArray(apps) ? apps : []) {
    const name = normalize(app?.name)
    const description = normalize(app?.description)
    const manifest = appManifestSearchDocument(app)
    const whole = `${name} ${description} ${manifest}`
    if (!includesEvery(whole, tokens)) continue

    let score = 100
    let matchArea = 'App details'
    const normalizedQuery = tokens.join(' ')
    if (name === normalizedQuery) {
      score = 500
      matchArea = 'Name'
    } else if (name.startsWith(normalizedQuery)) {
      score = 420
      matchArea = 'Name'
    } else if (includesEvery(name, tokens)) {
      score = 340
      matchArea = 'Name'
    } else if (includesEvery(description, tokens)) {
      score = 220
      matchArea = 'Description'
    }

    ranked.push({ app, matchArea, score })
  }

  return ranked
    .sort((left, right) => (
      right.score - left.score
      || String(left.app?.name || '').localeCompare(String(right.app?.name || ''))
      || Number(left.app?.id || 0) - Number(right.app?.id || 0)
    ))
    .slice(0, Math.max(0, limit))
    .map(({ score: _score, ...result }) => result)
}

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

// Selection history intentionally stores only stable item references. Titles,
// snippets, and the owner's query text remain in their owning data sources and
// are resolved afresh when the dialog opens.
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

export function resolvedSearchSelection(index, resultCount) {
  if (resultCount === 0) return -1
  return Math.min(Math.max(index, 0), resultCount - 1)
}

export function moveSearchSelection(index, key, resultCount) {
  const current = resolvedSearchSelection(index, resultCount)
  if (current === -1) return -1
  if (key === 'ArrowDown') return (current + 1) % resultCount
  if (key === 'ArrowUp') return (current - 1 + resultCount) % resultCount
  return current
}

export function visibleChatSearchState(chatState, query) {
  const normalizedQuery = String(query || '').trim()
  if (chatState?.query === normalizedQuery) return chatState
  return { query: normalizedQuery, status: 'loading', results: [] }
}

export function chatSearchResultIsCurrent(result, query) {
  return Boolean(result) && result.searchQuery === String(query || '').trim()
}

export function chatSearchOpenTarget(result) {
  return {
    view: 'chat',
    chatId: result.id,
    focusComposer: !result.anchor_key,
  }
}
