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

export function searchCommands(commands, query, limit = 12) {
  const tokens = queryTokens(query)
  const rows = Array.isArray(commands) ? commands : []
  if (!tokens.length) return rows.slice(0, Math.max(0, limit))
  return rows
    .filter(command => includesEvery(normalize([
      command?.title,
      command?.description,
      command?.category,
      ...(Array.isArray(command?.keywords) ? command.keywords : []),
      ...(Array.isArray(command?.shortcutLabels) ? command.shortcutLabels : []),
    ].filter(Boolean).join(' ')), tokens))
    .slice(0, Math.max(0, limit))
}

export function searchInstalledApps(apps, query, limit = 8, recentSelections = []) {
  const tokens = queryTokens(query)
  if (!tokens.length) return []
  const recentIndex = new Map(
    recentSelections
      .filter(selection => selection?.kind === 'app')
      .map((selection, index) => [String(selection.id), index]),
  )

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
      // Recency breaks equally relevant suggestions; it never lets a weaker
      // description/detail match outrank a stronger name match.
      || (recentIndex.get(String(left.app?.id)) ?? Infinity)
        - (recentIndex.get(String(right.app?.id)) ?? Infinity)
      || String(left.app?.name || '').localeCompare(String(right.app?.name || ''))
      || Number(left.app?.id || 0) - Number(right.app?.id || 0)
    ))
    .slice(0, Math.max(0, limit))
    .map(({ score: _score, ...result }) => result)
}

export function buildSearchResultGroups({
  query,
  commandResults = [],
  appResults = [],
  visibleChats = { status: 'idle', results: [] },
  recentSelections = [],
}) {
  if (!String(query || '').trim()) {
    return [
      ...(recentSelections.length ? [{
        headingId: 'global-search-recent-selections',
        listId: 'global-search-recent-selection-results',
        label: 'Recent selections',
        clearable: true,
        rows: recentSelections.map(({ kind, value }) => ({
          kind,
          value,
          recent: true,
          matchArea: 'Recent',
        })),
      }] : []),
      ...(commandResults.length ? [{
        headingId: 'global-search-commands',
        listId: 'global-search-command-results',
        label: 'Commands',
        rows: commandResults.map(command => ({ kind: 'command', value: command })),
      }] : []),
    ]
  }

  return [
    ...(commandResults.length ? [{
      headingId: 'global-search-commands',
      listId: 'global-search-command-results',
      label: 'Commands',
      rows: commandResults.map(command => ({ kind: 'command', value: command })),
    }] : []),
    ...(appResults.length ? [{
      headingId: 'global-search-apps',
      listId: 'global-search-app-results',
      label: 'Apps',
      rows: appResults.map(({ app, matchArea }) => ({
        kind: 'app', value: app, matchArea,
      })),
    }] : []),
    {
      headingId: 'global-search-chats',
      listId: 'global-search-chat-results',
      label: 'Chats',
      status: visibleChats.status,
      rows: visibleChats.results.map(result => ({ kind: 'chat', value: result })),
    },
  ]
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

export function pointerPositionChanged(previous, next) {
  return Boolean(previous && next) && (
    previous.x !== next.x || previous.y !== next.y
  )
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
