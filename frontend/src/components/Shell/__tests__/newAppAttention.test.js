import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  acknowledgeAppActivity,
  appAttentionIds,
  freshChatBuiltApps,
  freshAppIds,
  rememberSeenAppActivity,
  seenAppActivityVersion,
  withAppActivity,
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

test('an old acknowledgement cannot clear a newly reused app ID', () => {
  const rows = [{ id: 7, created_at: 'new-lifetime', has_unseen_activity: true, unseen_activity_version: 1 }]
  assert.equal(withAppActivitySeen(rows, 7, 1, 'old-lifetime'), rows)
  assert.equal(withAppActivitySeen(rows, 7, 1, 'new-lifetime')[0].has_unseen_activity, false)
})

test('another tab\'s seen receipt suppresses only the cleared lifetime and version', () => {
  const seen = new Map()
  rememberSeenAppActivity(seen, 7, 'current', 5)
  rememberSeenAppActivity(seen, 7, 'current', 4)
  assert.equal(seenAppActivityVersion(seen, 7, 'current'), 5)
  const rows = [{ id: 7, created_at: 'current', has_unseen_activity: false, unseen_activity_version: null }]
  assert.equal(withAppActivity(rows, 7, 5, { appCreatedAt: 'current', seenThrough: seenAppActivityVersion(seen, 7, 'current') }), rows)
  assert.equal(withAppActivity(rows, 7, 6, { appCreatedAt: 'current', seenThrough: seenAppActivityVersion(seen, 7, 'current') })[0].unseen_activity_version, 6)
  rememberSeenAppActivity(seen, 7, 'new-lifetime', 1)
  rememberSeenAppActivity(seen, 7, 'old-lifetime', 9)
  assert.equal(seenAppActivityVersion(seen, 7, 'current'), 5)
  assert.equal(seenAppActivityVersion(seen, 7, 'new-lifetime'), 1)
  assert.equal(withAppActivity(rows, 7, 5, { appCreatedAt: 'new-lifetime', seenThrough: 1 }), null)
})

test('acknowledgement deduplicates one exact app version and confirms the cache', async () => {
  const inFlight = new Set()
  const seen = new Map()
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
    appCreatedAt: 'current',
    inFlight,
    request,
    clearCached: (...args) => clears.push(args),
    confirmSeen: (id, version) => rememberSeenAppActivity(seen, id, 'current', version),
    restoreServerTruth: () => assert.fail('success must not restore server truth'),
  }

  const first = acknowledgeAppActivity(options)
  const duplicate = acknowledgeAppActivity(options)
  assert.equal(await duplicate, false)
  assert.equal(requests, 1)
  assert.deepEqual(clears, [[7, 3]])
  assert.equal(seenAppActivityVersion(seen, 7, 'current'), undefined)
  releaseRequest({ ok: true, status: 204 })
  assert.equal(await first, true)
  assert.equal(seenAppActivityVersion(seen, 7, 'current'), 3)
  assert.deepEqual(clears, [[7, 3], [7, 3]])
  assert.equal(inFlight.size, 0)
})

test('failed acknowledgement does not suppress a delayed activity event', async () => {
  const seen = new Map()
  let rows = [{ id: 7, created_at: 'current', has_unseen_activity: true, unseen_activity_version: 3 }]
  const acknowledged = await acknowledgeAppActivity({
    appId: 7,
    activityVersion: 3,
    appCreatedAt: 'current',
    inFlight: new Set(),
    request: async () => ({ ok: false, status: 503 }),
    clearCached: (id, version) => {
      rows = withAppActivitySeen(rows, id, version, 'current')
    },
    confirmSeen: (id, version) => rememberSeenAppActivity(seen, id, 'current', version),
    restoreServerTruth: async () => {},
  })
  assert.equal(acknowledged, false)
  assert.equal(seenAppActivityVersion(seen, 7, 'current'), undefined)
  rows = withAppActivity(rows, 7, 3, {
    appCreatedAt: 'current',
    seenThrough: seenAppActivityVersion(seen, 7, 'current'),
  })
  assert.equal(rows[0].has_unseen_activity, true)
  assert.equal(rows[0].unseen_activity_version, 3)
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


test('a live app_activity event marks only its app, without a list download', () => {
  const apps = [
    { id: 1, created_at: 'old', has_unseen_activity: false, unseen_activity_version: null },
    { id: 2, created_at: 'current', has_unseen_activity: false, unseen_activity_version: null },
  ]
  const next = withAppActivity(apps, '2', 7, { appCreatedAt: 'current' })
  assert.deepEqual(next[1], { id: 2, created_at: 'current', has_unseen_activity: true, unseen_activity_version: 7 })
  assert.equal(next[0], apps[0])
})

test('an event the shell cannot apply asks for a refetch instead of guessing', () => {
  const apps = [{ id: 1, created_at: 'current', has_unseen_activity: false }]
  assert.equal(withAppActivity(apps, 1, undefined), null)
  assert.equal(withAppActivity(apps, 99, 3, { appCreatedAt: 'current' }), null)
  assert.equal(withAppActivity(undefined, 1, 3, { appCreatedAt: 'current' }), null)
  assert.equal(withAppActivity(apps, 1, 3, { appCreatedAt: 'prior-lifetime' }), null)
  assert.equal(withAppActivity(apps, 1, 3), null, 'older event shape refetches instead of guessing an app lifetime')
})

test('already-acknowledged activity never re-lights the dot; newer activity does', () => {
  const apps = [{ id: 4, created_at: 'current', has_unseen_activity: false, unseen_activity_version: null }]
  assert.equal(withAppActivity(apps, 4, 5, { appCreatedAt: 'current', seenThrough: 5 }), apps)
  assert.equal(withAppActivity(apps, 4, 6, { appCreatedAt: 'current', seenThrough: 5 })[0].unseen_activity_version, 6)
})

test('a late event never lowers a newer cached activity version', () => {
  const apps = [{ id: 3, created_at: 'current', has_unseen_activity: true, unseen_activity_version: 9 }]
  assert.equal(withAppActivity(apps, 3, 8, { appCreatedAt: 'current' }), apps)
})

test('invalid marker versions and app identities request authoritative state', () => {
  const apps = [{ id: 1, created_at: 'current', has_unseen_activity: false }]
  for (const version of [null, '', 0, -1, 1.5, Infinity, NaN, undefined]) {
    assert.equal(withAppActivity(apps, 1, version, { appCreatedAt: 'current' }), null)
  }
  for (const id of [null, '', 0, -1, 1.5, Infinity, NaN, undefined]) {
    assert.equal(withAppActivity(apps, id, 3, { appCreatedAt: 'current' }), null)
  }
})

test('two app lifetimes with the same id and marker version do not share an acknowledgement key', async () => {
  const inFlight = new Set()
  const releases = []
  const options = {
    appId: 7, activityVersion: 1, inFlight,
    request: () => new Promise(resolve => releases.push(resolve)),
    clearCached: () => {}, restoreServerTruth: () => {},
  }
  const old = acknowledgeAppActivity({ ...options, appCreatedAt: 'old' })
  const current = acknowledgeAppActivity({ ...options, appCreatedAt: 'current' })
  assert.equal(releases.length, 2)
  releases.forEach(resolve => resolve({ ok: true }))
  assert.equal(await old, true)
  assert.equal(await current, true)
})
