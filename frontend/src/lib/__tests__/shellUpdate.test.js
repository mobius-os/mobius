import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  inspectShellUpdate,
  reloadIfGenerationStale,
  reloadWhenWorkerTakesOver,
  watchForShellUpdateOnForeground,
  SW_DISCOVERY_SETTLE_TIMEOUT_MS,
  SW_TAKEOVER_TIMEOUT_MS,
} from '../shellUpdate.js'

// Drain both promise continuations and the queued task where browsers publish a
// newly-discovered registration.installing worker after update() resolves.
const flush = async () => {
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
}

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
  const listeners = {}
  const reg = {
    waiting,
    active,
    installing,
    addEventListener(t, fn) { (listeners[t] ||= []).push(fn) },
    removeEventListener(t, fn) {
      listeners[t] = (listeners[t] || []).filter(f => f !== fn)
    },
    emit(t) { (listeners[t] || []).slice().forEach(fn => fn()) },
    count(t) { return (listeners[t] || []).length },
  }
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
  await flush()
  assert.equal(rearms, 1, 'reaching installed applies on the first return')
  installing.become('redundant') // a later transition must not re-fire
  assert.equal(rearms, 1)
  dispose()
})

test('watchForShellUpdateOnForeground: catches a worker published one task after update resolves', async () => {
  const active = { id: 'a' }
  const installing = makeInstalling('installing')
  let publishInstalling
  const published = new Promise(resolve => { publishInstalling = resolve })
  const reg = makeReg({ waiting: null, active })
  reg.update = async () => {
    setTimeout(() => {
      reg.installing = installing
      reg.emit('updatefound')
      publishInstalling()
    }, 0)
  }
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })

  doc.emit('visibilitychange')
  await published
  await flush()
  assert.equal(rearms, 0, 'the foreground check waits for the late-published install')

  reg.waiting = { id: 'w' }
  installing.become('installed')
  await flush()
  assert.equal(rearms, 1, 'the newly-published generation applies without another foreground event')
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
  await flush()
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
  await flush()
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
  await flush()
  assert.equal(rearms, 1, 'apply the surviving waiting A when the newer install fails')
  dispose()
})

test('watchForShellUpdateOnForeground: dispose removes listeners', async () => {
  const controller = { id: 'a' }
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  const doc = makeDoc('visible')
  let rearms = 0
  const dispose = watchForShellUpdateOnForeground({
    doc, win: null, serviceWorker: sw, rearm: () => { rearms += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(rearms, 0)
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

test('inspectShellUpdate waits for the newest install and returns its generation state', async () => {
  const workerA = { id: 'A' }
  const active = { id: 'active' }
  const installingB = makeInstalling('installing')
  let updates = 0
  const reg = makeReg({
    waiting: workerA,
    active,
    onUpdate: (current) => {
      updates += 1
      current.installing = installingB
    },
  })
  const sw = makeSwWith(reg, { controller: active })

  let result = null
  const inspection = inspectShellUpdate({
    serviceWorker: sw,
    setTimeoutFn: null,
  }).then(value => { result = value })
  await flush()
  assert.equal(updates, 1, 'one inspection owns the fresh generation check')
  assert.equal(result, null, 'the older waiting worker is not chosen while B installs')
  assert.equal(installingB.count('statechange'), 1)

  reg.waiting = { id: 'B' }
  installingB.become('installed')
  await inspection
  assert.equal(result.registration, reg)
  assert.equal(result.updateAvailable, true)
  assert.equal(reg.waiting.id, 'B')
  assert.equal(installingB.count('statechange'), 0)
})

test('inspectShellUpdate observes a worker published one task after update resolves', async () => {
  const active = { id: 'active' }
  const installingB = makeInstalling('installing')
  const queuedTasks = []
  const reg = makeReg({ waiting: null, active })
  const sw = makeSwWith(reg, { controller: active })
  let result = null

  const inspection = inspectShellUpdate({
    serviceWorker: sw,
    setTimeoutFn: fn => { queuedTasks.push(fn); return queuedTasks.length },
    clearTimeoutFn: () => {},
  }).then(value => { result = value })
  await flush()
  assert.equal(queuedTasks.length, 1, 'inspection yields for the queued registration update')

  reg.installing = installingB
  reg.emit('updatefound')
  queuedTasks.shift()()
  await flush()
  assert.equal(result, null, 'the late-published worker owns the inspection')

  reg.waiting = { id: 'B' }
  installingB.become('installed')
  await inspection
  assert.equal(result.updateAvailable, true)
  assert.equal(reg.count('updatefound'), 0)
})

test('inspectShellUpdate has a bounded escape from a wedged install', async () => {
  const installing = makeInstalling('installing')
  const reg = makeReg({ installing })
  const sw = makeSwWith(reg)
  const timers = fakeTimers()
  let result = null
  const inspection = inspectShellUpdate({
    serviceWorker: sw,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  }).then(value => { result = value })
  await flush()
  assert.equal(result, null)
  assert.equal(timers.count(), 1)
  timers.fire()
  await inspection
  assert.equal(result.registration, reg)
  assert.equal(installing.count('statechange'), 0)
})

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
  assert.ok(SW_DISCOVERY_SETTLE_TIMEOUT_MS >= 1000 && SW_DISCOVERY_SETTLE_TIMEOUT_MS <= 3000)
})

test('inspectShellUpdate reports a healthy controlled generation as current', async () => {
  const worker = { id: 'current' }
  const reg = makeReg({ active: worker })
  const result = await inspectShellUpdate({ serviceWorker: makeSwWith(reg, { controller: worker }) })
  assert.equal(result.registration, reg)
  assert.equal(result.updateAvailable, false)
})

test('inspectShellUpdate reports a waiting generation as available', async () => {
  const active = { id: 'active' }
  const reg = makeReg({ active, waiting: { id: 'waiting' } })
  const result = await inspectShellUpdate({ serviceWorker: makeSwWith(reg, { controller: active }) })
  assert.equal(result.updateAvailable, true)
})

test('inspectShellUpdate reports an active worker newer than the controller', async () => {
  const oldWorker = { id: 'N' }
  const newWorker = { id: 'N+1' }
  const reg = makeReg({ active: newWorker })
  const result = await inspectShellUpdate({
    serviceWorker: makeSwWith(reg, { controller: oldWorker }),
  })
  assert.equal(result.updateAvailable, true)
})

test('inspectShellUpdate treats first install and unavailable registration as current', async () => {
  const firstInstall = makeReg({ active: { id: 'first' } })
  assert.equal((await inspectShellUpdate({
    serviceWorker: makeSwWith(firstInstall, { controller: null }),
  })).updateAvailable, false)
  assert.deepEqual(await inspectShellUpdate({ serviceWorker: null }), {
    registration: null,
    updateAvailable: false,
  })
})

// --- reloadIfGenerationStale (recovery reload) ------------------------------

test('reloadIfGenerationStale: forces reg.update() before deciding staleness', async () => {
  let updated = 0
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active, onUpdate: () => { updated += 1 } })
  const sw = makeSwWith(reg, { controller: active })
  await reloadIfGenerationStale({ serviceWorker: sw, reload: () => {}, handoff: () => {} })
  assert.equal(updated, 1, 'a fresh sw.js fetch is forced so a just-shipped worker is discovered')
})

test('reloadIfGenerationStale: newer generation waiting → hands off and returns true', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  let handedOff = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: sw,
    reload: () => {},
    handoff: () => { handedOff += 1 },
  })
  assert.equal(healed, true, 'a waiting worker means a stale bundle → self-heal')
  assert.equal(handedOff, 1)
})

test('reloadIfGenerationStale: already newest generation → no reload, returns false', async () => {
  const controller = { id: 'a' }
  // active === controller, nothing waiting, no stale flag → current generation.
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  let handedOff = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: sw,
    reload: () => {},
    handoff: () => { handedOff += 1 },
  })
  assert.equal(healed, false, 'a genuine bug on the newest build must not auto-reload')
  assert.equal(handedOff, 0)
})

test('reloadIfGenerationStale: no service worker → returns false (no reload)', async () => {
  let handedOff = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: null,
    reload: () => {},
    handoff: () => { handedOff += 1 },
  })
  assert.equal(healed, false)
  assert.equal(handedOff, 0)
})
