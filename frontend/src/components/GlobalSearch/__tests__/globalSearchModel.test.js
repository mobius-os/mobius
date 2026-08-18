import assert from 'node:assert/strict'
import test from 'node:test'
import {
  appManifestSearchDocument,
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  clearRecentSelections,
  moveSearchSelection,
  readRecentSelections,
  rememberRecentSelection,
  resolveRecentSelections,
  resolvedSearchSelection,
  searchInstalledApps,
  visibleChatSearchState,
} from '../globalSearchModel.js'

class MemoryStorage {
  constructor(initial = {}) {
    this.values = new Map(Object.entries(initial))
  }

  getItem(key) {
    return this.values.get(key) ?? null
  }

  setItem(key, value) {
    this.values.set(key, String(value))
  }

  removeItem(key) {
    this.values.delete(key)
  }
}

const apps = [
  {
    id: 1,
    name: 'Morning Brief',
    description: 'A calm daily reading list.',
    slug: 'morning-brief',
    capability_contract: {
      background: { cron: '0 7 * * *', mode: 'scheduled' },
      agent: { skills: ['news.md'] },
    },
  },
  {
    id: 2,
    name: 'Newsroom',
    description: 'Follow stories and sources.',
    slug: 'newsroom',
    offline_capable: true,
    offline_contract: { reads: true, execution: 'none' },
  },
]

test('installed app search prioritizes names, then descriptions', () => {
  assert.deepEqual(
    searchInstalledApps(apps, 'news').map(result => [result.app.id, result.matchArea]),
    [[2, 'Name'], [1, 'App details']],
  )
  assert.deepEqual(searchInstalledApps(apps, 'calm'), [{
    app: apps[0],
    matchArea: 'Description',
  }])
})

test('manifest declarations stay searchable under partner-facing app details language', () => {
  assert.match(appManifestSearchDocument(apps[0]), /scheduled/)
  assert.deepEqual(searchInstalledApps(apps, 'cron scheduled'), [{
    app: apps[0],
    matchArea: 'App details',
  }])
  assert.deepEqual(searchInstalledApps(apps, 'offline reads'), [{
    app: apps[1],
    matchArea: 'App details',
  }])
})

test('false capability defaults never create manifest matches', () => {
  const ordinary = {
    id: 3,
    name: 'Notes',
    github_connect: false,
    capability_contract: { data: { github_connect: false } },
  }
  const connected = {
    id: 4,
    name: 'Contribute',
    github_connect: true,
    capability_contract: { data: { github_connect: true } },
  }
  assert.deepEqual(searchInstalledApps([ordinary, connected], 'github_connect'), [{
    app: connected,
    matchArea: 'App details',
  }])
})

test('all query terms must match and result limits are stable', () => {
  assert.deepEqual(searchInstalledApps(apps, 'news scheduled'), [{
    app: apps[0],
    matchArea: 'App details',
  }])
  assert.deepEqual(searchInstalledApps(apps, 'news missing'), [])
  assert.equal(searchInstalledApps(apps, 'news', 1).length, 1)
})

test('recent selections persist as a deduplicated most-recent-first list', () => {
  const storage = new MemoryStorage()
  assert.deepEqual(readRecentSelections(storage), [])

  rememberRecentSelection({ kind: 'chat', id: 'chat-1' }, storage)
  rememberRecentSelection({ kind: 'app', id: 42 }, storage)
  rememberRecentSelection({ kind: 'chat', id: 'chat-1' }, storage)

  assert.deepEqual(readRecentSelections(storage), [
    { kind: 'chat', id: 'chat-1' },
    { kind: 'app', id: '42' },
  ])

  for (let index = 0; index < 14; index += 1) {
    rememberRecentSelection({ kind: 'chat', id: `chat-${index}` }, storage)
  }
  const bounded = readRecentSelections(storage)
  assert.equal(bounded.length, 12)
  assert.deepEqual(bounded.slice(0, 2), [
    { kind: 'chat', id: 'chat-13' },
    { kind: 'chat', id: 'chat-12' },
  ])

  clearRecentSelections(storage)
  assert.deepEqual(readRecentSelections(storage), [])
})

test('recent selections resolve current items in selection order and omit missing ones', () => {
  const chats = [
    { id: 'chat-1', title: 'First chat' },
    { id: 'chat-2', title: 'Second chat' },
  ]
  const installedApps = [
    { id: 7, name: 'Memory' },
  ]
  const rows = resolveRecentSelections([
    { kind: 'app', id: '7' },
    { kind: 'chat', id: 'missing' },
    { kind: 'chat', id: 'chat-2' },
  ], chats, installedApps)

  assert.deepEqual(rows, [
    { kind: 'app', value: installedApps[0] },
    { kind: 'chat', value: chats[1] },
  ])
})

test('keyboard search selection starts at the first result and wraps with arrows', () => {
  assert.equal(resolvedSearchSelection(0, 3), 0)
  assert.equal(resolvedSearchSelection(-1, 3), 0)
  assert.equal(resolvedSearchSelection(8, 3), 2)
  assert.equal(resolvedSearchSelection(0, 0), -1)

  assert.equal(moveSearchSelection(0, 'ArrowDown', 3), 1)
  assert.equal(moveSearchSelection(2, 'ArrowDown', 3), 0)
  assert.equal(moveSearchSelection(0, 'ArrowUp', 3), 2)
  assert.equal(moveSearchSelection(2, 'ArrowUp', 3), 1)
  assert.equal(moveSearchSelection(0, 'ArrowDown', 0), -1)
})

test('a changed chat query hides and rejects stale result actions', () => {
  const staleState = {
    query: 'older',
    status: 'ready',
    results: [{ id: 'chat-1', searchQuery: 'older' }],
  }
  assert.deepEqual(visibleChatSearchState(staleState, 'newer'), {
    query: 'newer',
    status: 'loading',
    results: [],
  })
  assert.equal(chatSearchResultIsCurrent(staleState.results[0], 'newer'), false)
  assert.equal(chatSearchResultIsCurrent(staleState.results[0], ' older '), true)
})

test('chat destinations focus either the matched row or the ordinary composer', () => {
  assert.deepEqual(chatSearchOpenTarget({ id: 'chat-title', anchor_key: null }), {
    view: 'chat',
    chatId: 'chat-title',
    focusComposer: true,
  })
  assert.deepEqual(chatSearchOpenTarget({ id: 'chat-row', anchor_key: 'user-4' }), {
    view: 'chat',
    chatId: 'chat-row',
    focusComposer: false,
  })
})
