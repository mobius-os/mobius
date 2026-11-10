import test from 'node:test'
import assert from 'node:assert/strict'
import {
  tableScrollbarThumb,
  tableScrollForTrackDistance,
  tableScrollLeftForTrackTap,
} from '../markdown/tableScrollbar.js'

// A 300px column showing a 900px table; the track is as wide as the column.
const wide = { clientWidth: 300, scrollWidth: 900, trackWidth: 300 }

function assertNear(actual, expected) {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} is not ${expected}`)
}

/** Where the thumb sits on the track, in track pixels. */
function thumbSpan(scrollLeft) {
  const { widthPercent, offsetPercent } = tableScrollbarThumb({ ...wide, scrollLeft })
  const width = (widthPercent / 100) * wide.trackWidth
  const left = (offsetPercent / 100) * width
  return { left, right: left + width }
}

test('thumb is as wide as the visible share of the table', () => {
  assertNear(tableScrollbarThumb({ ...wide, scrollLeft: 0 }).widthPercent, 100 / 3)
})

test('thumb starts at the track start and ends at the track end when fully scrolled', () => {
  const start = thumbSpan(0)
  assertNear(start.left, 0)
  assertNear(start.right, 100)
  const end = thumbSpan(wide.scrollWidth - wide.clientWidth)
  assertNear(end.left, 200)
  assertNear(end.right, wide.trackWidth)
})

test('a table that fits, or has no width yet, gets a full idle thumb', () => {
  const full = { widthPercent: 100, offsetPercent: 0 }
  assert.deepEqual(tableScrollbarThumb({ clientWidth: 300, scrollWidth: 300, scrollLeft: 0 }), full)
  assert.deepEqual(tableScrollbarThumb({ clientWidth: 0, scrollWidth: 0, scrollLeft: 0 }), full)
})

test('dragging keeps the thumb under the pointer', () => {
  const scrolled = tableScrollForTrackDistance(40, wide)
  assert.equal(scrolled, 120)
  assertNear(thumbSpan(scrolled).left, 40)
  assert.equal(tableScrollForTrackDistance(40, { ...wide, trackWidth: 0 }), 0)
})

test('a track tap centres the thumb there, clamped to the table edges', () => {
  const centred = tableScrollLeftForTrackTap(150, wide)
  assert.equal(centred, 300)
  const span = thumbSpan(centred)
  assertNear((span.left + span.right) / 2, 150)
  assert.equal(tableScrollLeftForTrackTap(5, wide), 0)
  assert.equal(tableScrollLeftForTrackTap(299, wide), 600)
  assert.equal(tableScrollLeftForTrackTap(150, { ...wide, trackWidth: 0 }), 0)
})
