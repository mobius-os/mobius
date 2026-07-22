import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  acknowledgeAppActivity,
  appAttentionIds,
  freshChatBuiltApps,
  freshAppIds,
  withAppActivitySeen,
  withAppsFlagged,
  withoutAppFlagged,
} from '../newAppAttention.js'

test('appAttentionIds combines session arrivals with durable app activity', () => {
  const ids = appAttentionIds([
    { id: 1, has_unseen_activity: false },
    { id: '2', has_unseen_activity: true },
    { id: 3, has_unseen_activity: true },
  ], new Set([1, '2']))
  assert.deepEqual([...ids], [1, 2, 3])
})

test('appAttentionIds never marks an app that is already visible', () => {
  const ids = appAttentionIds([
    { id: 1, has_unseen_activity: true },
    { id: 2, has_unseen_activity: true },
  ], new Set([1, 3]), new Set(['1', 3]))
  assert.deepEqual([...ids], [2])
})

test('withAppActivitySeen clears only the matching durable flag', () => {
  const rows = [
    { id: 1, has_unseen_activity: true, unseen_activity_version: 4 },
    { id: 2, has_unseen_activity: true },
  ]
  const next = withAppActivitySeen(rows, '1', 4)
  assert.deepEqual(next, [
    { id: 1, has_unseen_activity: false, unseen_activity_version: null },
    { id: 2, has_unseen_activity: true },
  ])
  assert.equal(withAppActivitySeen(next, 1), next)
})

test('withAppActivitySeen never lets an older acknowledgement hide newer work', () => {
  const rows = [
    { id: 1, has_unseen_activity: true, unseen_activity_version: 5 },
  ]
  assert.equal(withAppActivitySeen(rows, 1, 4), rows)
})

test('acknowledgement deduplicates one exact app version and confirms the cache', async () => {
  const inFlight = new Set()
  const clears = []
  let releaseRequest
  let requests = 0
  const request = () => {
    requests += 1
    return new Promise(resolve => { releaseRequest = resolve })
  }
  const options = {
    appId: 7,
    activityVersion: 3,
    inFlight,
    request,
    clearCached: (...args) => clears.push(args),
    restoreServerTruth: () => assert.fail('success must not restore server truth'),
  }

  const first = acknowledgeAppActivity(options)
  const duplicate = acknowledgeAppActivity(options)
  assert.equal(await duplicate, false)
  assert.equal(requests, 1)
  assert.deepEqual(clears, [[7, 3]])
  releaseRequest({ ok: true, status: 204 })
  assert.equal(await first, true)
  assert.deepEqual(clears, [[7, 3], [7, 3]])
  assert.equal(inFlight.size, 0)
})

test('failed acknowledgement releases its key before server truth can retry', async () => {
  const inFlight = new Set()
  let attempts = 0
  let restores = 0
  const options = {
    appId: 7,
    activityVersion: 3,
    inFlight,
    request: async () => {
      attempts += 1
      return attempts === 1
        ? { ok: false, status: 503 }
        : { ok: true, status: 204 }
    },
    clearCached: () => {},
    restoreServerTruth: async () => {
      restores += 1
      assert.equal(inFlight.has('7:3'), false)
    },
  }

  assert.equal(await acknowledgeAppActivity(options), false)
  assert.equal(await acknowledgeAppActivity(options), true)
  assert.equal(attempts, 2)
  assert.equal(restores, 1)
  assert.equal(inFlight.size, 0)
})

test('freshAppIds returns only ids absent from the baseline', () => {
  const baseline = new Set([1, 2, 3])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3]), [])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3, 4]), [4])
  assert.deepEqual(freshAppIds(baseline, [5, 4]), [5, 4])
})

test('freshAppIds normalizes ids so a string route id does not double-count', () => {
  const baseline = new Set([7])
  assert.deepEqual(freshAppIds(baseline, ['7', 8]), [8])
  assert.deepEqual(freshAppIds([7], ['7']), [])
})

test('freshChatBuiltApps returns all fresh chat-owned artifacts in app order', () => {
  const apps = [
    { id: 7, chat_id: 'chat-a' },
    { id: 8, chat_id: null },
    { id: 9, chat_id: 'chat-b' },
  ]
  assert.deepEqual(freshChatBuiltApps(apps, [7, 8, 9]), [
    { appId: 7, chatId: 'chat-a' },
    { appId: 9, chatId: 'chat-b' },
  ])
})

test('freshChatBuiltApps ignores old apps, invalid ids, and store installs', () => {
  const apps = [
    { id: 7, chat_id: 'chat-a' },
    { id: 'bad', chat_id: 'chat-b' },
    { id: 8 },
  ]
  assert.deepEqual(freshChatBuiltApps(apps, [8, 9]), [])
})

test('withAppsFlagged adds ids and keeps the same reference on a no-op', () => {
  const prev = new Set([1])
  const added = withAppsFlagged(prev, [2, 3])
  assert.deepEqual([...added], [1, 2, 3])

  assert.equal(withAppsFlagged(prev, []), prev)
  assert.equal(withAppsFlagged(prev, [1]), prev)
})

test('withoutAppFlagged clears one id and no-ops when absent', () => {
  const prev = new Set([1, 2])
  const cleared = withoutAppFlagged(prev, 2)
  assert.deepEqual([...cleared], [1])

  assert.equal(withoutAppFlagged(prev, 9), prev)
  assert.equal(withoutAppFlagged(prev, '2') === prev, false)
  assert.deepEqual([...withoutAppFlagged(prev, '2')], [1])
})
