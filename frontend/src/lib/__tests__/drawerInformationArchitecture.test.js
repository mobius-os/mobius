import test from 'node:test'
import assert from 'node:assert/strict'

import {
  buildDrawerSections,
  filterInstalledApps,
  findDrawerMenuItem,
  reconcileStripOrder,
} from '../../components/Drawer/drawerInformationArchitecture.js'

test('drawer separates mixed pins from mixed recents ordered by activity', () => {
  const sections = buildDrawerSections([
    { id: 'empty', has_messages: false, updated_at: '2026-07-30' },
    { id: 'old-chat', has_messages: true, updated_at: '2026-07-20' },
    { id: 'new-chat', has_messages: true, activity_at: '2026-07-29' },
    { id: 'pinned-chat', has_messages: true, pinned_at: '2026-07-25', updated_at: '2026-07-01' },
  ], [
    { id: 1, created_at: '2026-07-01', last_opened_at: '2026-07-27' },
    { id: 2, created_at: '2026-07-02', pinned_at: '2026-07-26' },
  ])

  // Oldest pin first: the chat (pinned 07-25) sits above the app (pinned 07-26).
  assert.deepEqual(sections.pinned.map(entry => [entry.kind, entry.item.id]), [
    ['chat', 'pinned-chat'],
    ['app', 2],
  ])
  assert.deepEqual(sections.recents.map(entry => [entry.kind, entry.item.id]), [
    ['chat', 'new-chat'],
    ['chat', 'old-chat'],
  ])
  // Pinned apps stay in the Apps strip as well as Pinned.
  assert.deepEqual(sections.recentApps.map(app => app.id), [1, 2])
  assert.deepEqual(sections.apps.map(app => app.id), [2, 1])
})

test('ordinary chat order follows owner activity rather than generic row updates', () => {
  const sections = buildDrawerSections([
    {
      id: 'agent-finished',
      has_messages: true,
      activity_at: '2026-07-28T09:00:00Z',
      updated_at: '2026-07-28T12:00:00Z',
    },
    {
      id: 'owner-steered',
      has_messages: true,
      activity_at: '2026-07-28T11:00:00Z',
      updated_at: '2026-07-28T11:00:00Z',
    },
  ], [])

  assert.deepEqual(
    sections.recents.map(entry => entry.item.id),
    ['owner-steered', 'agent-finished'],
  )
})

test('app opens outrank bundle updates without changing catalogue order', () => {
  const sections = buildDrawerSections([], [
    {
      id: 1,
      created_at: '2026-07-01',
      updated_at: '2026-07-29T12:00:00Z',
      last_opened_at: '2026-07-29T13:00:00Z',
    },
    {
      id: 2,
      created_at: '2026-07-02',
      updated_at: '2026-07-29T12:30:00Z',
    },
  ])

  assert.deepEqual(sections.recents, [])
  assert.deepEqual(sections.recentApps.map(app => app.id), [1, 2])
  assert.deepEqual(sections.apps.map(app => app.id), [1, 2])
})

test('pinned order is stable across mixed timestamp formats (Z vs naive UTC)', () => {
  // After a drag, optimistic chat stamps carry a trailing Z while a refetched
  // app carries the server's naive-UTC form. Same instants must still interleave
  // by time, not by the accident of the suffix.
  const sections = buildDrawerSections([
    { id: 'chat-early', has_messages: true, pinned_at: '2026-07-27T10:00:00.000Z' },
    { id: 'chat-late', has_messages: true, pinned_at: '2026-07-27T10:00:02.000Z' },
  ], [
    { id: 9, created_at: '2026-07-01', pinned_at: '2026-07-27T10:00:01.123456' },
  ])

  assert.deepEqual(sections.pinned.map(entry => [entry.kind, entry.item.id]), [
    ['chat', 'chat-early'],
    ['app', 9],
    ['chat', 'chat-late'],
  ])
})

test('a freshly pinned item lands at the bottom of the pinned list', () => {
  // Three items already pinned in chronological order; the newest pin (the app,
  // stamped last) must render last so pinning appends rather than jumping to top.
  const sections = buildDrawerSections([
    { id: 'first-pinned', has_messages: true, pinned_at: '2026-07-25T09:00:00Z' },
    { id: 'second-pinned', has_messages: true, pinned_at: '2026-07-25T10:00:00Z' },
  ], [
    { id: 7, created_at: '2026-07-01', pinned_at: '2026-07-25T11:00:00Z' },
  ])

  assert.deepEqual(sections.pinned.map(entry => [entry.kind, entry.item.id]), [
    ['chat', 'first-pinned'],
    ['chat', 'second-pinned'],
    ['app', 7],
  ])
})

test('app search covers names, descriptions, and slugs without reordering', () => {
  const apps = [
    { id: 1, name: 'Metro Board', description: 'Live departures', slug: 'underground' },
    { id: 2, name: 'Atlas', description: 'Maps and places', slug: 'atlas' },
  ]

  assert.equal(filterInstalledApps(apps, '  LIVE  ')[0].id, 1)
  assert.equal(filterInstalledApps(apps, 'underground')[0].id, 1)
  assert.equal(filterInstalledApps(apps, 'places')[0].id, 2)
  assert.equal(filterInstalledApps(apps, 'missing').length, 0)
  assert.equal(filterInstalledApps(apps, ''), apps)
})

test('a drawer menu item becomes ordinary absence when its row disappears', () => {
  const chat = { id: 'chat-a', title: 'Chat A' }
  const app = { id: 7, name: 'Atlas' }

  assert.equal(findDrawerMenuItem({ kind: 'chat', id: chat.id }, [chat], [app]), chat)
  assert.equal(findDrawerMenuItem({ kind: 'app', id: app.id }, [chat], [app]), app)
  assert.equal(findDrawerMenuItem({ kind: 'chat', id: chat.id }, [], [app]), null)
  assert.equal(findDrawerMenuItem(null, [chat], [app]), null)
})

test('the Apps row keeps in-view apps in place and brings scrolled-to apps to the front', () => {
  const order = ['a', 'b', 'c', 'd', 'e', 'f', 'g']
  // Opening an app already visible without scrolling leaves the row alone.
  assert.deepEqual(reconcileStripOrder(order, ['c', 'a', 'b', 'd', 'e', 'f', 'g'], 'c', 5), order)
  // An app that needed scrolling moves to the front.
  assert.deepEqual(
    reconcileStripOrder(order, ['f', 'a', 'b', 'c', 'd', 'e', 'g'], 'f', 5),
    ['f', 'a', 'b', 'c', 'd', 'e', 'g'],
  )
  // New installs join at the front; uninstalled apps drop out.
  assert.deepEqual(reconcileStripOrder(order, ['n', 'a', 'b', 'c', 'd', 'e', 'f'], null, 5),
    ['n', 'a', 'b', 'c', 'd', 'e', 'f'])
  // A list that has not loaded yet keeps the remembered order.
  assert.deepEqual(reconcileStripOrder(order, [], null, 5), order)
})
