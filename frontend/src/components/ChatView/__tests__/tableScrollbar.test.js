import test from 'node:test'
import assert from 'node:assert/strict'
import {
  followTableScrollbarPointer,
  tableScrollbarThumb,
  tableScrollLeftForDrag,
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
  const scrolled = tableScrollLeftForDrag(0, 40, wide)
  assert.equal(scrolled, 120)
  assertNear(thumbSpan(scrolled).left, 40)
  assertNear(thumbSpan(tableScrollLeftForDrag(scrolled, -30, wide)).left, 10)
  assert.equal(tableScrollLeftForDrag(120, 40, { ...wide, trackWidth: 0 }), 120)
})

test('a drag past either end stops there and picks the thumb back up under the pointer', () => {
  // Grab the thumb at the start, drag 250px right (past the end), then 100px back.
  assert.equal(tableScrollLeftForDrag(0, 250, wide), 600)
  const back = tableScrollLeftForDrag(0, 150, wide)
  assertNear(thumbSpan(back).left, 150)
  // The same from the far end: past the start, then forward again.
  assert.equal(tableScrollLeftForDrag(600, -250, wide), 0)
  assertNear(thumbSpan(tableScrollLeftForDrag(600, -150, wide)).left, 50)
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

/** A press at `clientX` on a bar under the `wide` table, which starts at the
 * table's left edge; the thumb covers track pixels 0-100. */
function pressBar(clientX, pointerType) {
  const scroller = { ...wide, scrollLeft: 0 }
  const track = Object.assign(new EventTarget(), {
    clientWidth: wide.trackWidth,
    getBoundingClientRect: () => ({ left: 0 }),
    setPointerCapture() {},
  })
  const thumb = { getBoundingClientRect: () => ({ left: 0, right: 100 }) }
  followTableScrollbarPointer({ currentTarget: track, clientX, pointerId: 1, pointerType }, scroller, thumb)
  const send = (type, x = clientX) => track.dispatchEvent(Object.assign(new Event(type), { clientX: x }))
  return { scroller, send }
}

test('a swipe from the bar that the browser takes to scroll the chat leaves the table where it was', () => {
  for (const at of [50, 250]) {
    const { scroller, send } = pressBar(at, 'touch')
    send('pointermove', at + 4)
    send('pointercancel')
    send('lostpointercapture')
    assert.equal(scroller.scrollLeft, 0)
    send('pointermove', at + 60)
    assert.equal(scroller.scrollLeft, 0)
  }
})

test('a touch on the track jumps when it lifts, and a mouse press jumps at once', () => {
  const touch = pressBar(150, 'touch')
  assert.equal(touch.scroller.scrollLeft, 0)
  touch.send('pointerup')
  assert.equal(touch.scroller.scrollLeft, 300)

  const mouse = pressBar(150, 'mouse')
  assert.equal(mouse.scroller.scrollLeft, 300)
})

test('a sideways touch drag on the thumb moves the table until it lifts', () => {
  const { scroller, send } = pressBar(50, 'touch')
  send('pointermove', 90)
  assert.equal(scroller.scrollLeft, 120)
  send('pointerup', 90)
  send('lostpointercapture')
  send('pointermove', 150)
  assert.equal(scroller.scrollLeft, 120)
})
