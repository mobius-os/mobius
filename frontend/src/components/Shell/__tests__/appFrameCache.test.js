import assert from 'node:assert/strict'
import test from 'node:test'

import { deriveRenderedAppIds } from '../appFrameCache.js'

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
