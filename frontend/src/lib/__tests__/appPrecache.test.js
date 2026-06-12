import test from 'node:test'
import assert from 'node:assert/strict'

import {
  WARM_APP_LIMIT,
  mergeAppLru,
  parseStoredAppLru,
  selectAppsToWarm,
} from '../appPrecache.js'

const app = (id, pinnedAt = null) => ({ id, pinned_at: pinnedAt })

test('warm selection takes recents first, in LRU order', () => {
  const apps = [app(1), app(2), app(3)]
  const picked = selectAppsToWarm(apps, [3, 1])
  assert.deepEqual(picked.map(a => a.id), [3, 1])
})

test('warm selection appends pinned apps by newest pin', () => {
  const apps = [
    app(1, '2026-06-01T00:00:00'),
    app(2),
    app(3, '2026-06-10T00:00:00'),
  ]
  const picked = selectAppsToWarm(apps, [2])
  assert.deepEqual(picked.map(a => a.id), [2, 3, 1])
})

test('warm selection dedups an app that is both recent and pinned', () => {
  const apps = [app(1, '2026-06-10T00:00:00'), app(2)]
  const picked = selectAppsToWarm(apps, [1, 2])
  assert.deepEqual(picked.map(a => a.id), [1, 2])
})

test('warm selection skips recents no longer installed', () => {
  // A stale persisted LRU (app uninstalled since last session) must not
  // warm a dead route.
  const picked = selectAppsToWarm([app(2)], [99, 2])
  assert.deepEqual(picked.map(a => a.id), [2])
})

test('warm selection caps at the limit', () => {
  const apps = Array.from({ length: 10 }, (_, i) => app(i + 1, `2026-06-0${(i % 9) + 1}`))
  const picked = selectAppsToWarm(apps, [10, 9, 8, 7, 6, 5, 4, 3])
  assert.equal(picked.length, WARM_APP_LIMIT)
})

test('warm selection matches string and numeric ids', () => {
  // The persisted LRU round-trips through JSON; ids may come back as
  // strings while the live list carries numbers (or vice versa).
  const picked = selectAppsToWarm([app(7)], ['7'])
  assert.deepEqual(picked.map(a => a.id), [7])
})

test('LRU merge puts current entries first and dedups stored', () => {
  assert.deepEqual(mergeAppLru([3, 1], [1, 2]), [3, 1, 2])
})

test('LRU merge caps at the warm limit', () => {
  const merged = mergeAppLru([1, 2, 3, 4], [5, 6, 7, 8])
  assert.equal(merged.length, WARM_APP_LIMIT)
  assert.deepEqual(merged, [1, 2, 3, 4, 5, 6])
})

test('LRU merge with no stored history keeps the live list', () => {
  assert.deepEqual(mergeAppLru([2, 1], []), [2, 1])
})

test('stored LRU parse tolerates junk', () => {
  // localStorage survives releases; anything an older build left behind
  // must degrade to "no signal", never throw.
  assert.deepEqual(parseStoredAppLru(null), [])
  assert.deepEqual(parseStoredAppLru(''), [])
  assert.deepEqual(parseStoredAppLru('not json'), [])
  assert.deepEqual(parseStoredAppLru('{"a":1}'), [])
  assert.deepEqual(parseStoredAppLru('[1,"2",{"x":1},null]'), [1, '2'])
})
