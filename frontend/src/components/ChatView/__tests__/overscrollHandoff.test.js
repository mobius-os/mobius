import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  overscrollHandoffDelta,
  wheelDeltaPixels,
} from '../overscrollHandoff.js'

const scrollable = { scrollHeight: 600, clientHeight: 180 } // maxScroll = 420

test('hands the leftover downward delta to the transcript at the bottom edge', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: 40, scrollTop: 420, ...scrollable }),
    40,
  )
})

test('hands the leftover upward delta to the transcript at the top edge', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: -40, scrollTop: 0, ...scrollable }),
    -40,
  )
})

test('lets the field consume a downward gesture with room left below', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: 40, scrollTop: 100, ...scrollable }),
    0,
  )
})

test('lets the field consume an upward gesture with room left above', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: -40, scrollTop: 100, ...scrollable }),
    0,
  )
})

test('does not hand off at the bottom edge when the gesture reverses upward', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: -40, scrollTop: 420, ...scrollable }),
    0,
  )
})

test('never intervenes for a field that cannot scroll (native chaining works)', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: 40, scrollTop: 0, scrollHeight: 180, clientHeight: 180 }),
    0,
  )
})

test('tolerates sub-pixel geometry at the edges', () => {
  assert.equal(
    overscrollHandoffDelta({ delta: 30, scrollTop: 419.7, ...scrollable }),
    30,
  )
  assert.equal(
    overscrollHandoffDelta({ delta: -30, scrollTop: 0.3, ...scrollable }),
    -30,
  )
})

test('returns 0 for a zero delta or non-finite geometry', () => {
  assert.equal(overscrollHandoffDelta({ delta: 0, scrollTop: 420, ...scrollable }), 0)
  assert.equal(overscrollHandoffDelta({ delta: NaN, scrollTop: 0, ...scrollable }), 0)
  assert.equal(overscrollHandoffDelta({}), 0)
})

test('normalizes line-mode wheel input using the field line height', () => {
  assert.equal(
    wheelDeltaPixels({ deltaY: 3, deltaMode: 1, lineHeight: 18 }),
    54,
  )
})

test('normalizes page-mode wheel input using the field page height', () => {
  assert.equal(
    wheelDeltaPixels({ deltaY: -1, deltaMode: 2, pageHeight: 180 }),
    -180,
  )
})

test('keeps pixel-mode input unchanged and rejects invalid deltas', () => {
  assert.equal(wheelDeltaPixels({ deltaY: 12, deltaMode: 0 }), 12)
  assert.equal(wheelDeltaPixels({ deltaY: NaN, deltaMode: 0 }), 0)
})
