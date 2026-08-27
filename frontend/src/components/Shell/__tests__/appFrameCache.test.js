import assert from 'node:assert/strict'
import test from 'node:test'

import {
  BASE_APP_CACHE_MAX,
  HIGH_MEMORY_APP_CACHE_MAX,
  appFrameCacheMaxForDeviceMemory,
  deriveRenderedAppIds,
} from '../appFrameCache.js'

test('unknown and lower-memory devices keep the established six-frame budget', () => {
  assert.equal(appFrameCacheMaxForDeviceMemory(undefined), BASE_APP_CACHE_MAX)
  assert.equal(appFrameCacheMaxForDeviceMemory(4), BASE_APP_CACHE_MAX)
  assert.equal(BASE_APP_CACHE_MAX, 6)
})

test('the highest reported memory tier retains a larger warm working set', () => {
  assert.equal(appFrameCacheMaxForDeviceMemory(8), HIGH_MEMORY_APP_CACHE_MAX)
  assert.equal(HIGH_MEMORY_APP_CACHE_MAX, 10)
})

test('visible app frames remain mounted even when they exceed the warm limit', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set([8, 3, 5, 2, 7, 1, 4]),
      singleScreen: null,
      warmIds: [9, 10],
      max: 6,
    }),
    ['1', '2', '3', '4', '5', '7', '8'],
  )
})

test('the standard-mode app is pinned while builder is the visible scene', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set(),
      singleScreen: { kind: 'app', id: 12 },
      warmIds: [9, 12, 4, 3],
      max: 3,
    }),
    ['4', '9', '12'],
  )
})

test('warm app frames are deduplicated and bounded behind visible frames', () => {
  assert.deepEqual(
    deriveRenderedAppIds({
      visibleAppIds: new Set([6]),
      singleScreen: null,
      warmIds: ['6', 5, 5, 4, 3],
      max: 3,
    }),
    ['4', '5', '6'],
  )
})
