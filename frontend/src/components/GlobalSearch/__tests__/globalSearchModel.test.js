import assert from 'node:assert/strict'
import test from 'node:test'
import {
  appManifestSearchDocument,
  buildSearchResultGroups,
  chatSearchOpenTarget,
  chatSearchResultIsCurrent,
  moveSearchSelection,
  pointerPositionChanged,
  resolvedSearchSelection,
  searchCommands,
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

test('recency breaks equally relevant app suggestions without beating match quality', () => {
  const notes = [
    { id: 3, name: 'Daily Notes', description: 'Plain text' },
    { id: 4, name: 'Notes Archive', description: 'Plain text' },
    { id: 5, name: 'Archive', description: 'Includes notes' },
  ]
  const recent = [
    { kind: 'app', id: '4' },
    { kind: 'app', id: '5' },
  ]

  assert.deepEqual(
    searchInstalledApps(notes, 'notes', 8, recent).map(result => result.app.id),
    [4, 3, 5],
  )
})

test('command search covers action copy, keywords, and shortcut labels', () => {
  const commands = [
    {
      id: 'pane.newChat', title: 'New chat pane',
      description: 'Open a new chat beside the focused pane.',
      category: 'Tabs and panes', keywords: ['split'], shortcutLabels: ['⌘\\'],
    },
    {
      id: 'history.back', title: 'Go back', description: 'Previous destination.',
      category: 'Navigation', keywords: ['history'], shortcutLabels: ['⌘,'],
    },
  ]
  assert.deepEqual(searchCommands(commands, 'split pane'), [commands[0]])
  assert.deepEqual(searchCommands(commands, 'history'), [commands[1]])
  assert.deepEqual(searchCommands(commands, ''), commands)
})

test('recent destinations precede commands until a typed query restores search grouping', () => {
  const recent = [{ kind: 'app', value: apps[0] }]
  const commands = [{ id: 'chat.new', title: 'New chat' }]

  assert.deepEqual(buildSearchResultGroups({
    query: '',
    recentSelections: recent,
    commandResults: commands,
  }).map(group => group.label), ['Recent selections', 'Commands'])

  assert.deepEqual(buildSearchResultGroups({
    query: 'brief',
    recentSelections: recent,
    commandResults: commands,
    appResults: [{ app: apps[0], matchArea: 'Name' }],
    visibleChats: { status: 'ready', results: [{ id: 'chat-1' }] },
  }).map(group => group.label), ['Commands', 'Apps', 'Chats'])
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

test('a stationary pointer cannot replace the keyboard-selected result', () => {
  const initial = { x: 800, y: 420 }
  assert.equal(pointerPositionChanged(null, initial), false)
  assert.equal(pointerPositionChanged(initial, { x: 800, y: 420 }), false)
  assert.equal(pointerPositionChanged(initial, { x: 801, y: 420 }), true)
  assert.equal(pointerPositionChanged(initial, { x: 800, y: 419 }), true)
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
