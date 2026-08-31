import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  inspectShellUpdate,
  releaseWaitingShellUpdate,
  reloadIfGenerationStale,
  watchForShellUpdateOnResume,
  SW_DISCOVERY_SETTLE_TIMEOUT_MS,
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

test('watchForShellUpdateOnResume: a WAITING worker on return-to-visible reports availability once', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(reports, 1, 'a waiting worker reports on the first foreground return')
  dispose()
})

test('watchForShellUpdateOnResume: desktop focus return checks while visibility stays visible', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  const win = makeWin()
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })

  win.emit('focus')
  await flush()
  assert.equal(reports, 1, 'switching back to a still-visible desktop window reports the update')
  dispose()
})

test('watchForShellUpdateOnResume: pageshow checks a restored document', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  const win = makeWin()
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })

  win.emit('pageshow')
  await flush()
  assert.equal(reports, 1, 'a restored page reports the waiting generation')
  dispose()
})

test('watchForShellUpdateOnResume: no new generation is a NO-OP (no false availability)', async () => {
  const controller = { id: 'a' }
  // active === controller, nothing waiting, no stale flag → current generation.
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  const doc = makeDoc('visible')
  const win = makeWin()
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(reports, 0, 'a return with no new generation reports nothing')
  dispose()
})

test('watchForShellUpdateOnResume: a worker discovered by update() reports availability when it reaches installed', async () => {
  const active = { id: 'a' }
  const installing = makeInstalling('installing')
  // update() populates reg.installing (the just-discovered worker), still installing.
  const reg = makeReg({ waiting: null, active, onUpdate: (r) => { r.installing = installing } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(reports, 0, 'still installing → not yet reportable')
  // Simulate the install completing: the worker is now waiting (leashed).
  reg.waiting = { id: 'w' }
  installing.become('installed')
  await flush()
  assert.equal(reports, 1, 'reaching installed reports on the first return')
  installing.become('redundant') // a later transition must not re-fire
  assert.equal(reports, 1)
  dispose()
})

test('watchForShellUpdateOnResume: catches a worker published one task after update resolves', async () => {
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
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })

  doc.emit('visibilitychange')
  await published
  await flush()
  assert.equal(reports, 0, 'the foreground check waits for the late-published install')

  reg.waiting = { id: 'w' }
  installing.become('installed')
  await flush()
  assert.equal(reports, 1, 'the newly-published generation reports without another foreground event')
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

test('finding 1: near-simultaneous visibilitychange + online coalesce to ONE listener + ONE report', async () => {
  const active = { id: 'a' }
  const installing = makeInstalling('installing')
  const reg = makeReg({ waiting: null, active, onUpdate: (r) => { r.installing = installing } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  const win = makeWin()
  let reports = 0
  const dispose = watchForShellUpdateOnResume({ doc, win, serviceWorker: sw, onAvailable: () => { reports += 1 } })
  // Both triggers fire synchronously, before the first check's await resolves.
  doc.emit('visibilitychange')
  win.emit('online')
  await flush()
  // Coalesced: exactly ONE installing-statechange listener, not two.
  assert.equal(installing.count('statechange'), 1, 'one check ran, one listener attached')
  reg.waiting = { id: 'w' }
  installing.become('installed')
  await flush()
  assert.equal(reports, 1, 'exactly one availability report despite two concurrent triggers')
  dispose()
})

test('finding 1: sequential returns never double-report (performing/reported latch)', async () => {
  const active = { id: 'a' }
  const reg = makeReg({ waiting: { id: 'w' }, active })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({ doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 } })
  doc.emit('visibilitychange'); await flush()
  assert.equal(reports, 1)
  doc.emit('visibilitychange'); await flush() // a second return after availability was reported
  assert.equal(reports, 1, 'reported latch: no second availability report')
  dispose()
})

test('finding 2: waiting A + installing B settles on the NEWEST (no reload into A first)', async () => {
  const active = { id: 'a' }
  const workerA = { id: 'A' }              // older generation, already WAITING (leashed)
  const installingB = makeInstalling('installing') // newer generation still INSTALLING
  const reg = makeReg({ waiting: workerA, active, onUpdate: (r) => { r.installing = installingB } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({ doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 } })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(reports, 0, 'must NOT report the older waiting A while a newer B is installing')
  // B finishes installing → it is now the waiting generation, A superseded.
  reg.waiting = { id: 'B' }
  installingB.become('installed')
  await flush()
  assert.equal(reports, 1, 'reports exactly once for the newest generation (B)')
  dispose()
})

test('finding 2: a redundant install falls back to the still-waiting generation', async () => {
  const active = { id: 'a' }
  const workerA = { id: 'A' }
  const installingB = makeInstalling('installing')
  const reg = makeReg({ waiting: workerA, active, onUpdate: (r) => { r.installing = installingB } })
  const sw = makeSwWith(reg, { controller: active })
  const doc = makeDoc('visible')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({ doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 } })
  doc.emit('visibilitychange'); await flush()
  assert.equal(reports, 0)
  installingB.become('redundant') // B failed; A is still the newest good generation
  await flush()
  assert.equal(reports, 1, 'report the surviving waiting A when the newer install fails')
  dispose()
})

test('watchForShellUpdateOnResume: dispose removes listeners', async () => {
  const controller = { id: 'a' }
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  const doc = makeDoc('visible')
  const win = makeWin()
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })
  doc.emit('visibilitychange')
  await flush()
  assert.equal(reports, 0)
  assert.equal(doc.count('visibilitychange'), 1)
  assert.equal(win.count('focus'), 1)
  assert.equal(win.count('pageshow'), 1)
  assert.equal(win.count('online'), 1)
  dispose()
  assert.equal(doc.count('visibilitychange'), 0, 'dispose unwires the visibility listener')
  assert.equal(win.count('focus'), 0, 'dispose unwires the focus listener')
  assert.equal(win.count('pageshow'), 0, 'dispose unwires the pageshow listener')
  assert.equal(win.count('online'), 0, 'dispose unwires the online listener')
})

test('watchForShellUpdateOnResume: a HIDDEN visibilitychange does nothing', async () => {
  const reg = makeReg({ waiting: { id: 'w' } })
  const sw = makeSwWith(reg)
  const doc = makeDoc('hidden')
  let reports = 0
  const dispose = watchForShellUpdateOnResume({
    doc, win: null, serviceWorker: sw, onAvailable: () => { reports += 1 },
  })
  doc.emit('visibilitychange') // going hidden — must not check/report
  await flush()
  assert.equal(reports, 0)
  dispose()
})

test('watchForShellUpdateOnResume: no serviceWorker support → inert dispose', () => {
  const dispose = watchForShellUpdateOnResume({ doc: makeDoc(), serviceWorker: null, onAvailable: () => {} })
  assert.equal(typeof dispose, 'function')
  dispose() // must not throw
})

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
  let taskSequence = 0
  const reg = makeReg({ waiting: null, active })
  const sw = makeSwWith(reg, { controller: active })
  let result = null

  const inspection = inspectShellUpdate({
    serviceWorker: sw,
    setTimeoutFn: fn => {
      const id = ++taskSequence
      queuedTasks.push({ id, fn })
      return id
    },
    clearTimeoutFn: id => {
      const index = queuedTasks.findIndex(task => task.id === id)
      if (index >= 0) queuedTasks.splice(index, 1)
    },
  }).then(value => { result = value })
  await flush()
  assert.equal(queuedTasks.length, 1, 'inspection yields for the queued registration update')

  reg.installing = installingB
  reg.emit('updatefound')
  queuedTasks.shift().fn()
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

test('inspectShellUpdate cannot be stranded by registration.update itself', async () => {
  const active = { id: 'active' }
  const reg = makeReg({ active })
  const sw = makeSwWith(reg, { controller: active })
  const timers = fakeTimers()
  let updates = 0
  let result = null
  reg.update = () => {
    updates += 1
    return new Promise(() => {})
  }

  const inspection = inspectShellUpdate({
    serviceWorker: sw,
    timeoutMs: 25,
    setTimeoutFn: timers.setTimeoutFn,
    clearTimeoutFn: timers.clearTimeoutFn,
  }).then(value => { result = value })
  await flush()
  assert.equal(updates, 1)
  assert.equal(timers.count(), 1, 'the unresolved update call gets one deadline')

  timers.fire()
  await flush()
  assert.equal(timers.count(), 1, 'the queued installing-state read still runs')
  timers.fire()
  await inspection

  assert.equal(result.registration, reg)
  assert.equal(result.updateAvailable, false)
})

test('SW discovery has a sane bounded fallback', () => {
  assert.ok(SW_DISCOVERY_SETTLE_TIMEOUT_MS >= 1000 && SW_DISCOVERY_SETTLE_TIMEOUT_MS <= 3000)
})

test('releaseWaitingShellUpdate releases once without owning reload timing', () => {
  const posted = []
  releaseWaitingShellUpdate({
    waiting: { postMessage: message => posted.push(message) },
  })
  assert.deepEqual(posted, [{ type: 'SKIP_WAITING' }])
  assert.doesNotThrow(() => releaseWaitingShellUpdate(null))
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

test('inspectShellUpdate preserves a handoff that settles during update()', async () => {
  const oldWorker = { id: 'old' }
  const newWorker = { id: 'new' }
  const reg = makeReg({ active: oldWorker, waiting: newWorker })
  const sw = makeSwWith(reg, { controller: oldWorker })
  reg.update = async () => {
    reg.waiting = null
    reg.active = newWorker
    sw.controller = newWorker
  }

  const result = await inspectShellUpdate({ serviceWorker: sw })
  assert.equal(result.registration, reg)
  assert.equal(result.updateAvailable, true,
    'a document that witnessed a generation handoff still needs one navigation')
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
  await reloadIfGenerationStale({ serviceWorker: sw, reload: () => {} })
  assert.equal(updated, 1, 'a fresh sw.js fetch is forced so a just-shipped worker is discovered')
})

test('reloadIfGenerationStale: newer generation waiting → reloads and returns true', async () => {
  const active = { id: 'a' }
  const posted = []
  const reg = makeReg({
    waiting: { id: 'w', postMessage: message => posted.push(message) },
    active,
  })
  const sw = makeSwWith(reg, { controller: active })
  let reloads = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: sw,
    reload: () => { reloads += 1 },
  })
  assert.equal(healed, true, 'a waiting worker means a stale bundle → self-heal')
  assert.deepEqual(posted, [{ type: 'SKIP_WAITING' }])
  assert.equal(reloads, 1)
})

test('reloadIfGenerationStale: already newest generation → no reload, returns false', async () => {
  const controller = { id: 'a' }
  // active === controller, nothing waiting, no stale flag → current generation.
  const reg = makeReg({ waiting: null, active: controller })
  const sw = makeSwWith(reg, { controller })
  let reloads = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: sw,
    reload: () => { reloads += 1 },
  })
  assert.equal(healed, false, 'a genuine bug on the newest build must not auto-reload')
  assert.equal(reloads, 0)
})

test('reloadIfGenerationStale: no service worker → returns false (no reload)', async () => {
  let reloads = 0
  const healed = await reloadIfGenerationStale({
    serviceWorker: null,
    reload: () => { reloads += 1 },
  })
  assert.equal(healed, false)
  assert.equal(reloads, 0)
})
