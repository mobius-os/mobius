import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  acknowledgeAppPreview,
  builtAppsSignature,
  derivedBuiltApps,
  withAppPreviewSeen,
} from '../builtAppState.js'

const app = (id, name, updated_at, chat_id) => ({ id, name, updated_at, chat_id })
const derived = (id, name, updated_at) => ({
  id,
  name,
  updated_at,
  preview_seen_updated_at: null,
  preview_seen_final: false,
})

test('built apps are derived from the apps that carry this chat_id', () => {
  const apps = [
    app(7, 'Habits', '2026-07-12T00:00:00Z', 'chat-a'),
    app(9, 'Other', '2026-07-12T00:00:00Z', 'chat-b'),
  ]
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a'), [
    derived(7, 'Habits', '2026-07-12T00:00:00Z'),
  ])
  assert.deepEqual(derivedBuiltApps(apps, 'chat-c'), [])
})

test('a chat can own several built apps, oldest updated first', () => {
  const apps = [
    app(8, 'Habits', '2026-07-12T02:00:00Z', 'chat-a'),
    app(7, 'Notes', '2026-07-12T01:00:00Z', 'chat-a'),
  ]
  assert.deepEqual(derivedBuiltApps(apps, 'chat-a'), [
    derived(7, 'Notes', '2026-07-12T01:00:00Z'),
    derived(8, 'Habits', '2026-07-12T02:00:00Z'),
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

test('signature changes when the current build or final result is acknowledged', () => {
  const base = [app(7, 'Habits', 't1', 'chat-a')]
  const previewSeen = [{
    ...base[0], preview_seen_updated_at: 't1', preview_seen_final: false,
  }]
  const finalSeen = [{
    ...base[0], preview_seen_updated_at: 't1', preview_seen_final: true,
  }]
  assert.notEqual(
    builtAppsSignature(base, 'chat-a'),
    builtAppsSignature(previewSeen, 'chat-a'),
  )
  assert.notEqual(
    builtAppsSignature(previewSeen, 'chat-a'),
    builtAppsSignature(finalSeen, 'chat-a'),
  )
})

test('withAppPreviewSeen never lets a stale click hide a newer build', () => {
  const rows = [app(7, 'Habits', 't2', 'chat-a')]
  assert.equal(withAppPreviewSeen(rows, 7, 't1', true), rows)
  assert.deepEqual(withAppPreviewSeen(rows, 7, 't2', false), [{
    ...rows[0],
    preview_seen_updated_at: 't2',
    preview_seen_final: false,
  }])
})

test('a final acknowledgement promotes the same build monotonically', () => {
  const previewSeen = [{
    ...app(7, 'Habits', 't1', 'chat-a'),
    preview_seen_updated_at: 't1',
    preview_seen_final: false,
  }]
  const finalSeen = withAppPreviewSeen(previewSeen, 7, 't1', true)
  assert.deepEqual(finalSeen, [{
    ...previewSeen[0], preview_seen_final: true,
  }])
  assert.equal(withAppPreviewSeen(finalSeen, 7, 't1', false), finalSeen)
})

test('preview acknowledgement deduplicates an exact build phase', async () => {
  const inFlight = new Set()
  const clears = []
  let release
  let requests = 0
  const options = {
    app: app(7, 'Habits', 't1', 'chat-a'),
    final: false,
    inFlight,
    request: () => {
      requests += 1
      return new Promise(resolve => { release = resolve })
    },
    clearCached: (...args) => clears.push(args),
    restoreServerTruth: () => assert.fail('success must not restore'),
  }
  const first = acknowledgeAppPreview(options)
  const duplicate = acknowledgeAppPreview(options)
  assert.equal(await duplicate, false)
  assert.equal(requests, 1)
  assert.deepEqual(clears, [[7, 't1', false]])
  release({ ok: true, status: 204 })
  assert.equal(await first, true)
  assert.deepEqual(clears, [[7, 't1', false], [7, 't1', false]])
})
