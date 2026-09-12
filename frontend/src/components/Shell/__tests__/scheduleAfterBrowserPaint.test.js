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

function lifecycleTarget(extra = {}) {
  const listeners = new Map()
  return {
    ...extra,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(listener)
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener)
    },
    emit(type) {
      for (const listener of listeners.get(type) || []) listener()
    },
    listenerCount(type) {
      return listeners.get(type)?.size || 0
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

test('a hidden tab re-arms the complete paint proof when it returns', () => {
  const frames = frameHarness()
  const documentTarget = lifecycleTarget({ visibilityState: 'visible' })
  const windowTarget = lifecycleTarget()
  let promotions = 0

  scheduleAfterBrowserPaint(
    () => { promotions += 1 },
    callback => frames.request(callback),
    id => frames.cancel(id),
    { documentTarget, windowTarget },
  )

  frames.paintFrame()
  assert.equal(frames.pending(), 1, 'the second frame is waiting to promote')

  documentTarget.visibilityState = 'hidden'
  documentTarget.emit('visibilitychange')
  assert.equal(frames.pending(), 0, 'the suspended frame is retired')

  documentTarget.visibilityState = 'visible'
  documentTarget.emit('visibilitychange')
  frames.paintFrame()
  assert.equal(promotions, 0, 'the returned tab still paints beneath the cover')
  frames.paintFrame()

  assert.equal(promotions, 1)
  assert.equal(documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(windowTarget.listenerCount('pageshow'), 0)
})

test('pageshow replaces a lost paint callback without double promotion', () => {
  const frames = frameHarness()
  const documentTarget = lifecycleTarget({ visibilityState: 'visible' })
  const windowTarget = lifecycleTarget()
  let promotions = 0

  scheduleAfterBrowserPaint(
    () => { promotions += 1 },
    callback => frames.request(callback),
    id => frames.cancel(id),
    { documentTarget, windowTarget },
  )

  windowTarget.emit('pageshow')
  frames.paintFrame()
  frames.paintFrame()
  windowTarget.emit('pageshow')
  frames.paintFrame()

  assert.equal(promotions, 1)
})
