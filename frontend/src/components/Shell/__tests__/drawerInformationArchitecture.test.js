import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  buildDrawerSections,
  findDrawerMenuItem,
  nextPendingPinnedAt,
  projectPendingDrawerPins,
} from '../../Drawer/drawerInformationArchitecture.js'

test('projects never join Recents; they open from the Projects screen', () => {
  const unopened = { id: 'p-unopened', name: 'Unopened', updated_at: '2026-08-26T08:00:00' }
  const opened = {
    id: 'p-opened',
    name: 'Opened',
    updated_at: '2026-08-25T08:00:00',
    last_opened_at: '2026-08-26T09:00:00',
  }

  const result = buildDrawerSections([], [], [unopened, opened])

  assert.deepEqual(result.recents, [])
})

test('built artifacts never join Recents; they live inside their project', () => {
  const project = {
    id: 'project-1', name: 'Album', updated_at: '2026-08-26T08:00:00',
    artifacts: [
      { id: 'unopened', name: 'Unopened', status: 'ok', has_output: true },
      {
        id: 'opened', name: 'Opened', status: 'ok', has_output: true,
        last_opened_at: '2026-08-26T10:00:00',
      },
    ],
  }

  const result = buildDrawerSections([], [], [project])

  assert.deepEqual(result.recents, [])
})

test('a pinned project stays on the Projects screen, out of drawer Pinned and Recents', () => {
  const project = {
    id: 'pinned-project',
    name: 'Pinned project',
    last_opened_at: '2026-08-26T09:00:00',
    pinned_at: '2026-08-26T10:00:00',
  }
  const app = {
    id: 7,
    name: 'Pinned app',
    pinned_at: '2026-08-26T09:00:00',
  }

  const result = buildDrawerSections([], [app], [project])

  assert.deepEqual(result.pinned.map(row => `${row.kind}:${row.item.id}`), [
    'app:7',
  ])
  assert.equal(result.recents.some(row => row.item.id === project.id), false)
})

test('the shared row menu resolves project identities', () => {
  const project = { id: 'project-1', name: 'Project one' }
  assert.equal(
    findDrawerMenuItem({ kind: 'project', id: project.id }, [], [], [project]),
    project,
  )
})

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
