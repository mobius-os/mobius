import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  reloadWhenWorkerTakesOver,
  shouldRearmShellApply,
  SW_TAKEOVER_TIMEOUT_MS,
} from '../swHandoff.js'

// Minimal event-emitter fakes so the SW handoff wiring is testable without a
// live service worker.
function makeWorker(state = 'installed') {
  const listeners = {}
  return {
    state,
    posted: [],
    postMessage(msg) { this.posted.push(msg) },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn) },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter(f => f !== fn)
    },
    emit(type) { (listeners[type] || []).slice().forEach(fn => fn()) },
    count(type) { return (listeners[type] || []).length },
  }
}
function makeSw() {
  const listeners = {}
  return {
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn) },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter(f => f !== fn)
    },
    emit(type) { (listeners[type] || []).slice().forEach(fn => fn()) },
    count(type) { return (listeners[type] || []).length },
  }
}
function fakeTimers() {
  let seq = 0
  let pending = []
  return {
    setTimeoutFn: (fn, ms) => { const id = ++seq; pending.push({ id, fn, ms }); return id },
    clearTimeoutFn: (id) => { pending = pending.filter(t => t.id !== id) },
    fire: () => { const t = pending.shift(); if (t) t.fn() },
    count: () => pending.length,
  }
}

test('reloadWhenWorkerTakesOver: no waiting worker reloads immediately', () => {
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting: null },
    serviceWorker: makeSw(),
    reload: () => { reloads += 1 },
  })
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: missing registration reloads immediately', () => {
  let reloads = 0
  reloadWhenWorkerTakesOver({ registration: undefined, reload: () => { reloads += 1 } })
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: posts SKIP_WAITING and reloads only after activation', () => {
  const waiting = makeWorker('installed')
  const sw = makeSw()
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting },
    serviceWorker: sw,
    reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  })
  // The one and only handoff message goes to the waiting worker.
  assert.deepEqual(waiting.posted, [{ type: 'SKIP_WAITING' }])
  assert.equal(reloads, 0, 'must not reload before the worker takes over')
  // An intermediate transition is not a takeover.
  waiting.state = 'activating'; waiting.emit('statechange')
  assert.equal(reloads, 0)
  // 'activated' is the takeover signal — reload now.
  waiting.state = 'activated'; waiting.emit('statechange')
  assert.equal(reloads, 1)
  // Listeners + timer torn down so nothing fires twice.
  assert.equal(waiting.count('statechange'), 0)
  assert.equal(sw.count('controllerchange'), 0)
  assert.equal(timers.count(), 0)
})

test('reloadWhenWorkerTakesOver: a controllerchange also triggers the reload', () => {
  const waiting = makeWorker('installed')
  const sw = makeSw()
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting }, serviceWorker: sw, reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn,
  })
  sw.emit('controllerchange')
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: a redundant worker still reloads (re-arm net recovers)', () => {
  const waiting = makeWorker('installed')
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting }, serviceWorker: makeSw(), reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn,
  })
  waiting.state = 'redundant'; waiting.emit('statechange')
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: the bounded timeout reloads a wedged handoff', () => {
  const waiting = makeWorker('installed')
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting }, serviceWorker: makeSw(), reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn,
  })
  assert.equal(reloads, 0)
  timers.fire() // SW never activated — fallback fires
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: reloads exactly once even if several signals fire', () => {
  const waiting = makeWorker('installed')
  const sw = makeSw()
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting }, serviceWorker: sw, reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn,
  })
  waiting.state = 'activated'; waiting.emit('statechange')
  sw.emit('controllerchange')       // no-op after settle
  waiting.emit('statechange')       // no-op after settle
  timers.fire()                     // already cleared — no-op
  assert.equal(reloads, 1)
})

test('reloadWhenWorkerTakesOver: a worker already activated at attach reloads immediately', () => {
  const waiting = makeWorker('activated')
  const timers = fakeTimers()
  let reloads = 0
  reloadWhenWorkerTakesOver({
    registration: { waiting }, serviceWorker: makeSw(), reload: () => { reloads += 1 },
    setTimeoutFn: timers.setTimeoutFn, clearTimeoutFn: timers.clearTimeoutFn,
  })
  // The post-attach guard catches a worker that raced past 'waiting'.
  assert.equal(reloads, 1)
  assert.equal(timers.count(), 0)
})

test('SW_TAKEOVER_TIMEOUT_MS is a sane bounded fallback', () => {
  assert.ok(SW_TAKEOVER_TIMEOUT_MS >= 1000 && SW_TAKEOVER_TIMEOUT_MS <= 3000)
})

test('shouldRearmShellApply: healthy page (controller is the active worker) does not re-arm', () => {
  const w = {}
  assert.equal(shouldRearmShellApply({ active: w, controller: w }), false)
  assert.equal(shouldRearmShellApply({}), false)
  // Uncontrolled page (first install in progress) is not "stale".
  assert.equal(shouldRearmShellApply({ active: {}, controller: null }), false)
  // Active-less registration (no SW) is not "stale".
  assert.equal(shouldRearmShellApply({ active: null, controller: {} }), false)
})

test('shouldRearmShellApply: a stale-precache flag re-arms', () => {
  assert.equal(shouldRearmShellApply({ stalePrecacheFlagged: true }), true)
})

test('shouldRearmShellApply: a waiting worker re-arms (lost apply signal)', () => {
  assert.equal(shouldRearmShellApply({ waiting: {} }), true)
})

test('shouldRearmShellApply: an active worker newer than the controller re-arms (feature 207)', () => {
  const oldWorker = { id: 'N' }
  const newWorker = { id: 'N+1' }
  // reg.waiting is null in the settled 207 state — the identity mismatch is the
  // only signal, and it must re-arm.
  assert.equal(shouldRearmShellApply({
    waiting: null, active: newWorker, controller: oldWorker,
  }), true)
})
