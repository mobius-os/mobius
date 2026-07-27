import test from 'node:test'
import assert from 'node:assert/strict'

import {
  appInitials,
  buildDrawerSections,
  filterInstalledApps,
} from '../../components/Drawer/drawerInformationArchitecture.js'

test('drawer separates pinned chats and apps from ordinary chat history', () => {
  const sections = buildDrawerSections([
    { id: 'empty', has_messages: false, updated_at: '2026-07-30' },
    { id: 'old-chat', has_messages: true, updated_at: '2026-07-20' },
    { id: 'new-chat', has_messages: true, activity_at: '2026-07-29' },
    { id: 'pinned-chat', has_messages: true, pinned_at: '2026-07-25', updated_at: '2026-07-01' },
  ], [
    { id: 1, created_at: '2026-07-01' },
    { id: 2, created_at: '2026-07-02', pinned_at: '2026-07-26' },
  ])

  assert.deepEqual(sections.pinned.map(entry => [entry.kind, entry.item.id]), [
    ['app', 2],
    ['chat', 'pinned-chat'],
  ])
  assert.deepEqual(sections.chats.map(chat => chat.id), ['new-chat', 'old-chat'])
  assert.deepEqual(sections.apps.map(app => app.id), [2, 1])
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

test('app initials remain useful for missing custom icons', () => {
  assert.equal(appInitials('Beat Machine'), 'BM')
  assert.equal(appInitials('Atlas'), 'AT')
  assert.equal(appInitials('---'), 'A')
})
