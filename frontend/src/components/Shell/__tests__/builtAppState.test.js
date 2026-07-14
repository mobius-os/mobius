import { test } from 'node:test'
import assert from 'node:assert/strict'

import { builtAppsSignature, derivedBuiltApps } from '../builtAppState.js'

const app = (id, name, updated_at, chat_id) => ({ id, name, updated_at, chat_id })

test('built apps are derived from the apps that carry this chat_id', () => {
  const apps = [
    app(7, 'Habits', '2026-07-12T00:00:00Z', 'chat-a'),
    app(9, 'Other', '2026-07-12T00:00:00Z', 'chat-b'),
  ]
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a'), [
    { id: 7, name: 'Habits', updated_at: '2026-07-12T00:00:00Z' },
  ])
  assert.deepEqual(derivedBuiltApps(apps, 'chat-c'), [])
})

test('a chat can own several built apps, oldest updated first', () => {
  const apps = [
    app(8, 'Habits', '2026-07-12T02:00:00Z', 'chat-a'),
    app(7, 'Notes', '2026-07-12T01:00:00Z', 'chat-a'),
  ]
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a'), [
    { id: 7, name: 'Notes', updated_at: '2026-07-12T01:00:00Z' },
    { id: 8, name: 'Habits', updated_at: '2026-07-12T02:00:00Z' },
  ])
})

test('only the newest three owned apps are kept', () => {
  const apps = [1, 2, 3, 4].map(
    i => app(i, `A${i}`, `2026-07-12T0${i}:00:00Z`, 'chat-a'))
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a').map(a => a.id), [2, 3, 4])
})

test('an app tombstoned (dropped from the apps list) leaves the CTA', () => {
  // The list is DERIVED, so an uninstalled app simply is not in `apps` and
  // its CTA disappears with no prune step.
  const apps = [app(7, 'Alive', '2026-07-12T00:00:00Z', 'chat-a')]
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a').map(a => a.id), [7])
  assert.deepEqual(derivedBuiltApps([], 'chat-a'), [])
})

test('a chat that owns no apps always yields the same list reference', () => {
  // ChatView relies on this stable identity so its list-keyed effects do not
  // fire for chats that built nothing.
  assert.equal(derivedBuiltApps([], 'chat-a'), derivedBuiltApps([], 'chat-b'))
})

test('null/empty chat id yields the empty list', () => {
  const apps = [app(7, 'Habits', '2026-07-12T00:00:00Z', 'chat-a')]
  assert.deepEqual(derivedBuiltApps(apps, null), [])
  assert.deepEqual(derivedBuiltApps(apps, ''), [])
  assert.deepEqual(derivedBuiltApps(undefined, 'chat-a'), [])
})

test('chat_id is matched string-normalized', () => {
  const apps = [app(7, 'Habits', '2026-07-12T00:00:00Z', 7)]
  assert.deepEqual(derivedBuiltApps(apps, '7').map(a => a.id), [7])
})

test('signature is stable across an unrelated app_updated refetch', () => {
  // The load-bearing invariant: a fresh `apps` array whose relevant content is
  // unchanged (e.g. another chat's app bumped) must yield the SAME signature,
  // so Shell's memo returns the same reference and ChatView effects don't fire.
  const a1 = [
    app(7, 'Habits', '2026-07-12T01:00:00Z', 'chat-a'),
    app(9, 'Other', '2026-07-12T01:00:00Z', 'chat-b'),
  ]
  const a2 = [
    app(7, 'Habits', '2026-07-12T01:00:00Z', 'chat-a'),
    app(9, 'Other', '2026-07-12T09:00:00Z', 'chat-b'), // bumped, other chat
  ]
  assert.equal(builtAppsSignature(a1, 'chat-a'), builtAppsSignature(a2, 'chat-a'))
})

test('signature changes when this chat owns a new app or a recompile', () => {
  const base = [app(7, 'Habits', '2026-07-12T01:00:00Z', 'chat-a')]
  const recompiled = [app(7, 'Habits', '2026-07-12T02:00:00Z', 'chat-a')]
  const added = [
    app(7, 'Habits', '2026-07-12T01:00:00Z', 'chat-a'),
    app(8, 'Notes', '2026-07-12T03:00:00Z', 'chat-a'),
  ]
  assert.notEqual(
    builtAppsSignature(base, 'chat-a'), builtAppsSignature(recompiled, 'chat-a'))
  assert.notEqual(
    builtAppsSignature(base, 'chat-a'), builtAppsSignature(added, 'chat-a'))
})
