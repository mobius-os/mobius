import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  appTileLabel,
  buildDrawerSections,
  findDrawerMenuItem,
  nextPendingPinnedAt,
  projectPendingDrawerPins,
  wheelRowScrollDelta,
} from '../../Drawer/drawerInformationArchitecture.js'

test('a pending chat pin stays in Pinned across an older list snapshot', () => {
  const staleChat = {
    id: 'chat-1', title: 'Race-safe chat', has_messages: true,
    activity_at: '2026-09-12T12:00:00', pinned_at: null,
  }
  const pending = new Map([[
    'chat:chat-1',
    { pinned: true, pinnedAt: '2026-09-12T12:01:00.000Z' },
  ]])

  const projected = projectPendingDrawerPins([staleChat], [], [], pending)
  const result = buildDrawerSections(
    projected.chats,
    projected.apps,
    projected.projects,
  )

  assert.deepEqual(result.pinned.map(row => `${row.kind}:${row.item.id}`), [
    'chat:chat-1',
  ])
  assert.equal(result.recents.some(row => row.item.id === staleChat.id), false)
})

test('a newer pending unpin wins over a stale pinned list snapshot', () => {
  const staleApp = {
    id: 7, name: 'Race-safe app', created_at: '2026-09-12T12:00:00',
    pinned_at: '2026-09-12T12:01:00',
  }
  const pending = new Map([[
    'app:7',
    { pinned: false, pinnedAt: null },
  ]])

  const projected = projectPendingDrawerPins([], [staleApp], [], pending)
  const result = buildDrawerSections(
    projected.chats,
    projected.apps,
    projected.projects,
  )

  assert.equal(result.pinned.some(row => row.item.id === staleApp.id), false)
  assert.deepEqual(result.recentApps.map(app => app.id), [7])
})

// The Projects list renders this projection, so a project pin or unpin still
// in flight is what the list shows, whatever an older list read says.
test('a pending project pin or unpin overrides an older project list', () => {
  const stalePinned = { id: 'p-1', name: 'Was pinned', pinned_at: '2026-09-12T12:01:00' }
  const staleUnpinned = { id: 'p-2', name: 'Was unpinned', pinned_at: null }
  const pending = new Map([
    ['project:p-1', { pinned: false, pinnedAt: null }],
    ['project:p-2', { pinned: true, pinnedAt: '2026-09-12T12:02:00.000Z' }],
  ])

  const { projects } = projectPendingDrawerPins([], [], [stalePinned, staleUnpinned], pending)

  assert.deepEqual(projects.map(row => [row.id, row.pinned_at]), [
    ['p-1', null],
    ['p-2', '2026-09-12T12:02:00.000Z'],
  ])
})

test('a pending pin appends even when the browser clock is behind', () => {
  const chats = [{
    id: 'chat-1', title: 'Existing pin', has_messages: true,
    pinned_at: '2031-01-01T00:00:00.999900',
  }]

  assert.equal(
    nextPendingPinnedAt(chats, [], [], {
      now: Date.parse('2026-09-12T12:00:00Z'),
    }),
    '2031-01-01T00:00:01.000Z',
  )
})

test('rapid pending pins receive distinct append ranks', () => {
  const first = nextPendingPinnedAt([], [], [], {
    now: Date.parse('2026-09-12T12:00:00Z'),
  })
  const second = nextPendingPinnedAt([], [], [], {
    now: Date.parse('2026-09-12T12:00:00Z'),
    previous: first,
  })

  assert.equal(first, '2026-09-12T12:00:00.000Z')
  assert.equal(second, '2026-09-12T12:00:00.001Z')
})

test('archived chats leave Pinned and Recents for Archived, newest archive first', () => {
  const active = { id: 'active', has_messages: true, activity_at: '2026-09-29T09:00:00' }
  const olderArchive = {
    id: 'older', has_messages: true, activity_at: '2026-09-29T10:00:00',
    archived_at: '2026-09-20T08:00:00',
  }
  const newerArchive = {
    id: 'newer', has_messages: true, activity_at: '2026-09-01T10:00:00',
    archived_at: '2026-09-28T08:00:00Z',
  }
  const staleArchivedPin = {
    id: 'stale-pin', has_messages: true, pinned_at: '2026-09-02T08:00:00',
    archived_at: '2026-09-10T08:00:00',
  }

  const result = buildDrawerSections(
    [active, olderArchive, newerArchive, staleArchivedPin], [], [],
  )

  assert.deepEqual(result.pinned, [])
  assert.deepEqual(result.recents.map(row => row.item.id), ['active'])
  assert.deepEqual(result.archived.map(row => row.item.id), ['newer', 'older', 'stale-pin'])
})

test('project chats stay out of Recents but keep an explicit pin', () => {
  const project = { id: 'p1', name: 'Thesis' }
  const plain = { id: 'plain', has_messages: true, activity_at: '2026-11-05T09:00:00' }
  const inProject = {
    id: 'in-project', has_messages: true, activity_at: '2026-11-05T10:00:00', project,
  }
  const pinnedInProject = {
    id: 'pinned-in-project', has_messages: true, project,
    pinned_at: '2026-11-05T08:00:00',
  }

  const result = buildDrawerSections([plain, inProject, pinnedInProject], [], [])

  assert.deepEqual(result.recents.map(row => row.item.id), ['plain'])
  assert.deepEqual(result.pinned.map(row => row.item.id), ['pinned-in-project'])
})

test('an empty archive is an empty Archived section', () => {
  const result = buildDrawerSections([{ id: 'a', has_messages: true }], [], [])
  assert.deepEqual(result.archived, [])
})

// Screen readers hear the same status the Apps-row dot shows.
test('an Apps-row icon is announced with the status its dot shows', () => {
  assert.equal(appTileLabel('Notes'), 'Notes')
  assert.equal(appTileLabel('Notes', { needsYou: true, fresh: true }), 'Notes, needs you')
  assert.equal(appTileLabel('Notes', { fresh: true }), 'Notes, new')
  assert.equal(appTileLabel('Notes', { needsYou: true, building: true }), 'Notes, needs you, building')
  assert.equal(appTileLabel(''), 'App')
})

// A plain mouse wheel scrolls the collapsed Apps row sideways, but never traps
// the wheel: at either end, and for sideways or zoom input, the browser keeps it.
test('a mouse wheel scrolls the Apps row sideways until it reaches an end', () => {
  const row = { clientWidth: 200, scrollWidth: 500, scrollLeft: 100 }
  assert.equal(wheelRowScrollDelta({ deltaY: 40 }, row), 40)
  assert.equal(wheelRowScrollDelta({ deltaY: -40 }, row), -40)
  assert.equal(wheelRowScrollDelta({ deltaY: 3, deltaMode: 1 }, row), 48, 'line steps become pixels')

  assert.equal(wheelRowScrollDelta({ deltaY: -40 }, { ...row, scrollLeft: 0 }), 0, 'start: the sidebar scrolls')
  assert.equal(wheelRowScrollDelta({ deltaY: 40 }, { ...row, scrollLeft: 300 }), 0, 'end: the sidebar scrolls')
  assert.equal(wheelRowScrollDelta({ deltaY: 40 }, { ...row, scrollWidth: 200 }), 0, 'nothing to scroll')
  assert.equal(wheelRowScrollDelta({ deltaX: 30, deltaY: 10 }, row), 0, 'trackpad sideways stays native')
  assert.equal(wheelRowScrollDelta({ deltaY: 40, shiftKey: true }, row), 0, 'Shift+wheel stays native')
  assert.equal(wheelRowScrollDelta({ deltaY: 40, ctrlKey: true }, row), 0, 'pinch zoom stays native')
})
