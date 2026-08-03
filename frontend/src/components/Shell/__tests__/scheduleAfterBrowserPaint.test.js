import { test } from 'node:test'
import assert from 'node:assert/strict'

import { scheduleAfterBrowserPaint } from '../scheduleAfterBrowserPaint.js'

function frameHarness() {
  let nextId = 1
  const callbacks = new Map()
  return {
    request(callback) {
      const id = nextId++
      callbacks.set(id, callback)
      return id
    },
    cancel(id) {
      callbacks.delete(id)
    },
    paintFrame() {
      const frame = [...callbacks.entries()]
      callbacks.clear()
      for (const [, callback] of frame) callback()
    },
    pending() {
      return callbacks.size
    },
  }
}

test('chat promotion follows one prepared browser paint opportunity', () => {
  const frames = frameHarness()
  let promoted = false
  scheduleAfterBrowserPaint(
    () => { promoted = true },
    callback => frames.request(callback),
    id => frames.cancel(id),
  )

  assert.equal(promoted, false)
  frames.paintFrame()
  assert.equal(promoted, false, 'the destination paints beneath its cover here')
  frames.paintFrame()
  assert.equal(promoted, true, 'promotion follows that prepared frame')
})

test('a superseded staging chat cannot promote after cancellation', () => {
  const frames = frameHarness()
  let promotions = 0
  const cancel = scheduleAfterBrowserPaint(
    () => { promotions += 1 },
    callback => frames.request(callback),
    id => frames.cancel(id),
  )

  frames.paintFrame()
  cancel()
  frames.paintFrame()
  assert.equal(promotions, 0)
  assert.equal(frames.pending(), 0)
})
