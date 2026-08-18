import assert from 'node:assert/strict'
import test from 'node:test'
import {
  appManifestSearchDocument,
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  clearLastSearch,
  moveSearchSelection,
  recentApps,
  recentChats,
  readLastSearch,
  rememberLastSearch,
  resolvedSearchSelection,
  searchInstalledApps,
  visibleChatSearchState,
} from '../globalSearchModel.js'

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

test('the empty search view starts with recent chats, then recently opened apps', () => {
  const chats = [
    { id: 'empty', title: 'Empty', has_messages: false, activity_at: '2026-08-18T13:00:00Z' },
    { id: 'older', title: 'Older', has_messages: true, activity_at: '2026-08-18T11:00:00Z' },
    { id: 'newer', title: 'Newer', has_messages: true, activity_at: '2026-08-18T12:00:00Z' },
  ]
  assert.deepEqual(recentChats(chats).map(chat => chat.id), ['newer', 'older'])
  assert.deepEqual(chats.map(chat => chat.id), ['empty', 'older', 'newer'])

  const installed = [
    { id: 1, name: 'Older', last_opened_at: '2026-08-17T12:00:00Z' },
    { id: 2, name: 'Newest', last_opened_at: '2026-08-18T12:00:00Z' },
    { id: 3, name: 'Never opened', created_at: '2026-08-16T12:00:00Z' },
  ]
  assert.deepEqual(recentApps(installed, 2).map(app => app.id), [2, 1])
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

test('the last search survives a close/reopen so the term does not have to be retyped', () => {
  clearLastSearch()
  assert.deepEqual(readLastSearch(), {
    query: '',
    chatState: { query: '', status: 'idle', results: [] },
  })

  const settled = {
    query: 'password',
    status: 'ready',
    results: [{ id: 'chat-1', searchQuery: 'password' }],
  }
  rememberLastSearch('password', settled)

  // What a reopened dialog seeds its state from: the term AND the results the
  // owner was looking at, so the list is on screen before any refetch lands.
  const restored = readLastSearch()
  assert.equal(restored.query, 'password')
  assert.equal(restored.chatState.status, 'ready')
  assert.deepEqual(restored.chatState.results, settled.results)
  assert.deepEqual(visibleChatSearchState(restored.chatState, restored.query), settled)
  // The restored rows stay clickable: the staleness guard keys off the term the
  // dialog reopens with, not a fresh empty one.
  assert.equal(chatSearchResultIsCurrent(restored.chatState.results[0], restored.query), true)

  clearLastSearch()
})

test('an unsettled search is not restored, only its term', () => {
  clearLastSearch()
  for (const status of ['loading', 'error', 'idle']) {
    rememberLastSearch('half typed', { query: 'half typed', status, results: [] })
    const restored = readLastSearch()
    assert.equal(restored.query, 'half typed', `${status} keeps the term`)
    assert.equal(restored.chatState.status, 'idle', `${status} is not replayed`)
    assert.deepEqual(restored.chatState.results, [])
  }
  clearLastSearch()
})
