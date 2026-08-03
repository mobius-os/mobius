import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  reloadWhenWorkerTakesOver,
  shouldRearmShellApply,
  watchForShellUpdateOnForeground,
  SW_TAKEOVER_TIMEOUT_MS,
} from '../swHandoff.js'

// Deterministic drain of the pending microtask queue (getRegistration/update are
// awaited inside the watch). Two awaits cover getRegistration → update → decide.
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

function makeDoc(visibilityState = 'visible') {
  const listeners = {}
  return {
    visibilityState,
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn) },
    removeEventListener(t, fn) { listeners[t] = (listeners[t] || []).filter(f => f !== fn) },
    emit(t) { (listeners[t] || []).slice().forEach(fn => fn()) },
    count(t) { return (listeners[t] || []).length },
  }
}
function makeInstalling(state = 'installing') {
  const listeners = {}
  return {
    state,
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn) },
    removeEventListener(t, fn) { listeners[t] = (listeners[t] || []).filter(f => f !== fn) },
    become(next) { this.state = next; (listeners.statechange || []).slice().forEach(fn => fn()) },
    count(t) { return (listeners[t] || []).length },
  }
}
// A serviceWorker fake whose getRegistration resolves to `reg` and whose reg.update
// runs an optional side effect (e.g. populate reg.installing / reg.waiting).
function makeSwWith(reg, { controller = null, onUpdate } = {}) {
  return {
    controller,
    async getRegistration() { return reg },
    _reg: reg,
    _onUpdate: onUpdate,
  }
}
function makeReg({ waiting = null, active = null, installing = null, onUpdate } = {}) {
  const reg = { waiting, active, installing }
  reg.update = async () => { if (onUpdate) onUpdate(reg) }
  return reg
}

test('watchForShellUpdateOnForeground: a WAITING worker on return-to-visible re-arms once', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 1, 'a waiting worker applies on the first foreground return')
  dispose()
})

test('watchForShellUpdateOnForeground: no new generation is a NO-OP (no spurious reload)', async () => {
  const controller = { id: 'a' }
  // active === controller, nothing waiting, no stale flag → current generation.
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 0, 'a return with no new generation never reloads')
  dispose()
})

test('watchForShellUpdateOnForeground: a worker discovered by update() re-arms when it reaches installed', async () => {
  const active = { id: 'a' }
  const installing = makeInstalling('installing')
  // update() populates reg.installing (the just-discovered worker), still installing.
  const reg = makeReg({ waiting: null, active, onUpdate: (r) => { r.installing = installing } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 0, 'still installing → not yet applied')
  // Simulate the install completing: the worker is now waiting (leashed).
  reg.waiting = { id: 'w' }
  installing.become('installed')
  assert.equal(rearms, 1, 'reaching installed applies on the first return')
  installing.become('redundant') // a later transition must not re-fire
  assert.equal(rearms, 1)
  dispose()
})

function makeWin() {
  const listeners = {}
  return {
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn) },
    removeEventListener(t, fn) { listeners[t] = (listeners[t] || []).filter(f => f !== fn) },
    emit(t) { (listeners[t] || []).slice().forEach(fn => fn()) },
    count(t) { return (listeners[t] || []).length },
  }
}

test('finding 1: near-simultaneous visibilitychange + online coalesce to ONE listener + ONE rearm', async () => {
  const active = { id: 'a' }
  const installing = makeInstalling('installing')
  const reg = makeReg({ waiting: null, active, onUpdate: (r) => { r.installing = installing } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  const win = makeWin()
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({ doc, win, serviceWorker: sw, rearm: () => { rearms += 1 } })
  // Both triggers fire synchronously, before the first check's await resolves.
  doc.emit('visibilitychange')
  win.emit('online')
  await flush()
  // Coalesced: exactly ONE installing-statechange listener, not two.
  assert.equal(installing.count('statechange'), 1, 'one check ran, one listener attached')
  reg.waiting = { id: 'w' }
  installing.become('installed')
  assert.equal(rearms, 1, 'exactly one rearm despite two concurrent triggers')
  dispose()
})

test('finding 1: sequential returns never double-rearm (performing/applied latch)', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({ doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 } })
  doc.emit('visibilitychange'); await flush()
  assert.equal(rearms, 1)
  doc.emit('visibilitychange'); await flush() // a second return after the apply was requested
  assert.equal(rearms, 1, 'applied latch: no second rearm/reload')
  dispose()
})

test('finding 2: waiting A + installing B settles on the NEWEST (no reload into A first)', async () => {
  const active = { id: 'a' }
  const workerA = { id: 'A' }              // older generation, already WAITING (leashed)
  const installingB = makeInstalling('installing') // newer generation still INSTALLING
  const reg = makeReg({ waiting: workerA, active, onUpdate: (r) => { r.installing = installingB } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({ doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 } })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 0, 'must NOT apply the older waiting A while a newer B is installing')
  // B finishes installing → it is now the waiting generation, A superseded.
  reg.waiting = { id: 'B' }
  installingB.become('installed')
  assert.equal(rearms, 1, 'applies exactly once, on the newest generation (B)')
  dispose()
})

test('finding 2: a redundant install falls back to the still-waiting generation', async () => {
  const active = { id: 'a' }
  const workerA = { id: 'A' }
  const installingB = makeInstalling('installing')
  const reg = makeReg({ waiting: workerA, active, onUpdate: (r) => { r.installing = installingB } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({ doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 } })
  doc.emit('visibilitychange'); await flush()
  assert.equal(rearms, 0)
  installingB.become('redundant') // B failed; A is still the newest good generation
  assert.equal(rearms, 1, 'apply the surviving waiting A when the newer install fails')
  dispose()
})

test('watchForShellUpdateOnForeground: the stale-precache flag re-arms; dispose removes listeners', async () => {
  const controller = { id: 'a' }
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, readStaleFlag: () => true, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 1, 'a stale-precache flag alone re-arms')
  assert.equal(doc.count('visibilitychange'), 1)
  dispose()
  assert.equal(doc.count('visibilitychange'), 0, 'dispose unwires the visibility listener')
})

test('watchForShellUpdateOnForeground: a HIDDEN visibilitychange does nothing', async () => {
  const reg = makeReg({ waiting: { id: 'w' } })
  const sw = makeSwWith(reg)
  const doc = makeDoc('hidden')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange') // going hidden — must not check/apply
  await flush()
  assert.equal(rearms, 0)
  dispose()
})

test('watchForShellUpdateOnForeground: no serviceWorker support → inert dispose', () => {
  const dispose = watchForShellUpdateOnForeground({ doc: makeDoc(), serviceWorker: null, rearm: () => {} })
  assert.equal(typeof dispose, 'function')
  dispose() // must not throw
})

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
