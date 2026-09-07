import assert from 'node:assert/strict'
import test from 'node:test'

import useOpenAppCtaAutoDismiss from '../useOpenAppCtaAutoDismiss.js'
import { renderHook } from './react-hook-shim.mjs'

const app = (overrides = {}) => ({
  id: 42,
  name: 'Habits',
  updated_at: 'build-1',
  preview_seen_updated_at: null,
  preview_seen_final: false,
  ...overrides,
})

function fakeTimers() {
  let nextId = 1
  const scheduled = new Map()
  const cleared = []
  return {
    scheduled,
    cleared,
    setTimer(fn, delay) {
      const id = nextId++
      scheduled.set(id, { fn, delay })
      return id
    },
    clearTimer(id) {
      cleared.push(id)
      scheduled.delete(id)
    },
    fire(id) {
      const timer = scheduled.get(id)
      scheduled.delete(id)
      timer?.fn()
    },
  }
}

function fakeEventTarget(visibilityState) {
  const listeners = new Map()
  return {
    visibilityState,
    addEventListener(type, listener) {
      const current = listeners.get(type) || new Set()
      current.add(listener)
      listeners.set(type, current)
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

function args(overrides = {}) {
  return {
    builtApps: [app()],
    turnActive: true,
    presented: true,
    onDismissApp() {},
    ...overrides,
  }
}

test('the clock starts only when the shortcut is presented on a foreground page', () => {
  const timers = fakeTimers()
  const documentTarget = fakeEventTarget('hidden')
  const windowTarget = fakeEventTarget()
  const io = { ...timers, documentTarget, windowTarget }
  const firstDismiss = []
  const latestDismiss = []
  const hookArgs = args({
    presented: false,
    onDismissApp: value => firstDismiss.push(value),
  })
  const hook = renderHook(useOpenAppCtaAutoDismiss, hookArgs, io)

  // Neither a covered surface nor a background page has been observed.
  assert.equal(timers.scheduled.size, 0)
  hook.rerender({ ...hookArgs, presented: true }, io)
  assert.equal(timers.scheduled.size, 0)
  documentTarget.emit('visibilitychange')
  assert.equal(timers.scheduled.size, 0)

  documentTarget.visibilityState = 'visible'
  windowTarget.emit('pageshow')

  assert.equal(timers.scheduled.size, 1)
  const [timerId, timer] = [...timers.scheduled.entries()][0]
  assert.equal(timer.delay, 5000)

  // Turn completion, a later cover, and callback replacement do not restart a
  // clock whose shortcut the owner already saw.
  hook.rerender({
    ...hookArgs,
    presented: false,
    turnActive: false,
    onDismissApp: value => latestDismiss.push(value),
  }, io)
  assert.equal(timers.scheduled.size, 1)
  assert.deepEqual(timers.cleared, [])

  timers.fire(timerId)
  assert.deepEqual(firstDismiss, [])
  assert.deepEqual(latestDismiss, [hookArgs.builtApps[0]])
})

test('other previews keep their clocks while a replacement build gets a new one', () => {
  const timers = fakeTimers()
  const first = app()
  const hookArgs = args({ builtApps: [first] })
  const hook = renderHook(useOpenAppCtaAutoDismiss, hookArgs, timers)
  const [firstTimerId] = timers.scheduled.keys()

  const second = app({ id: 43, updated_at: 'build-2' })
  hook.rerender({ ...hookArgs, builtApps: [first, second] }, timers)

  assert.equal(timers.scheduled.size, 2)
  assert.equal(timers.scheduled.has(firstTimerId), true)
  assert.deepEqual(timers.cleared, [])

  const secondTimerId = [...timers.scheduled.keys()]
    .find(timerId => timerId !== firstTimerId)
  const replacement = app({ updated_at: 'build-3' })
  hook.rerender({ ...hookArgs, builtApps: [replacement, second] }, timers)

  assert.equal(timers.scheduled.size, 2)
  assert.equal(timers.scheduled.has(firstTimerId), false)
  assert.equal(timers.scheduled.has(secondTimerId), true)
  assert.deepEqual(timers.cleared, [firstTimerId])
})

test('opening a preview cancels its pending retirement', () => {
  const timers = fakeTimers()
  const hookArgs = args()
  const hook = renderHook(useOpenAppCtaAutoDismiss, hookArgs, timers)
  const [timerId] = timers.scheduled.keys()

  hook.rerender({
    ...hookArgs,
    builtApps: [app({ preview_seen_updated_at: 'build-1' })],
  }, timers)

  assert.equal(timers.scheduled.size, 0)
  assert.deepEqual(timers.cleared, [timerId])
})

test('unmount releases the page listeners and every pending clock', () => {
  const timers = fakeTimers()
  const documentTarget = fakeEventTarget('visible')
  const windowTarget = fakeEventTarget()
  const io = { ...timers, documentTarget, windowTarget }
  const hook = renderHook(useOpenAppCtaAutoDismiss, args(), io)

  assert.equal(timers.scheduled.size, 1)
  assert.equal(documentTarget.listenerCount('visibilitychange'), 1)
  assert.equal(windowTarget.listenerCount('pageshow'), 1)

  hook.unmount()
  assert.equal(timers.scheduled.size, 0)
  assert.equal(documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(windowTarget.listenerCount('pageshow'), 0)
})
