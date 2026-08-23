import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AMBIGUOUS_VERDICT_CONFIRM_MS,
  createConnectivityStore,
  ReachabilityPhase,
} from '../connectivityStore.js'

function eventTarget(extra = {}) {
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

function fakeTimers() {
  let nextId = 1
  const timeouts = new Map()
  const intervals = new Map()
  return {
    setTimeoutFn(callback, delay) {
      const id = nextId++
      timeouts.set(id, { callback, delay })
      return id
    },
    clearTimeoutFn(id) { timeouts.delete(id) },
    setIntervalFn(callback, delay) {
      const id = nextId++
      intervals.set(id, { callback, delay })
      return id
    },
    clearIntervalFn(id) { intervals.delete(id) },
    runTimeout(delay) {
      const found = [...timeouts].find(([, task]) => task.delay === delay)
      assert.ok(found, `expected a ${delay}ms timeout`)
      const [id, task] = found
      timeouts.delete(id)
      task.callback()
    },
    timeoutCount: () => timeouts.size,
    intervalCount: () => intervals.size,
  }
}

async function flushMicrotasks() {
  for (let index = 0; index < 6; index += 1) await Promise.resolve()
}

function harness(fetchImpl) {
  const windowTarget = eventTarget()
  const documentTarget = eventTarget({ visibilityState: 'visible' })
  const navigatorTarget = { onLine: true }
  const timers = fakeTimers()
  const store = createConnectivityStore({
    windowTarget,
    documentTarget,
    navigatorTarget,
    fetchImpl,
    ...timers,
  })
  return { store, windowTarget, documentTarget, navigatorTarget, timers }
}

test('all subscribers share one monitor and the last unsubscribe removes it', async () => {
  const h = harness(async () => ({ ok: true }))
  const stopA = h.store.subscribe(() => {})
  const stopB = h.store.subscribe(() => {})
  await flushMicrotasks()

  assert.equal(h.timers.intervalCount(), 1)
  assert.equal(h.windowTarget.listenerCount('online'), 1)
  assert.equal(h.windowTarget.listenerCount('offline'), 1)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 1)

  stopA()
  assert.equal(h.timers.intervalCount(), 1)
  stopB()
  assert.equal(h.timers.intervalCount(), 0)
  assert.equal(h.timers.timeoutCount(), 0)
  assert.equal(h.windowTarget.listenerCount('online'), 0)
  assert.equal(h.windowTarget.listenerCount('offline'), 0)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
})

test('a stale-online failure publishes checking before it is confirmed offline', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.CHECKING)
  assert.equal(notifications, 1)
  h.timers.runTimeout(AMBIGUOUS_VERDICT_CONFIRM_MS)
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), false)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.OFFLINE)
  assert.equal(notifications, 2)
  stop()
})

test('stopping the last subscriber cancels a pending confirmation probe', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  const stop = h.store.subscribe(() => {})
  await flushMicrotasks()

  assert.equal(h.timers.timeoutCount(), 1)
  stop()
  assert.equal(h.timers.timeoutCount(), 0)
  assert.equal(h.timers.intervalCount(), 0)
})

test('verification without subscribers is bounded and never starts a monitor', async () => {
  const h = harness(async () => ({ ok: true }))
  assert.equal(await h.store.verify(), true)

  assert.equal(h.timers.intervalCount(), 0)
  assert.equal(h.timers.timeoutCount(), 0)
  assert.equal(h.windowTarget.listenerCount('online'), 0)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
})

test('a live mutation response repairs a stale offline verdict immediately', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()
  h.timers.runTimeout(AMBIGUOUS_VERDICT_CONFIRM_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), false)

  h.store.reportReachable()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(h.store.getRecoverySnapshot(), 1)
  assert.equal(notifications, 3)
  stop()
})

test('a live mutation response outranks an older in-flight failed probe', async () => {
  let rejectProbe
  const h = harness(() => new Promise((resolve, reject) => { rejectProbe = reject }))
  h.navigatorTarget.onLine = false
  const stop = h.store.subscribe(() => {})

  h.store.reportReachable()
  rejectProbe(new TypeError('offline'))
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.timers.timeoutCount(), 0)
  stop()
})

test('any HTTP response proves transport reachability regardless of status', async () => {
  const h = harness(async () => ({ ok: false, status: 503 }))
  const stop = h.store.subscribe(() => {})
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  stop()
})

test('verification exposes uncertainty immediately and recovery emits one generation', async () => {
  let reachable = true
  const h = harness(async () => {
    if (!reachable) throw new TypeError('offline')
    return { ok: true }
  })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()

  reachable = false
  const failedCheck = h.store.verify()
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.CHECKING)
  assert.equal(h.store.getSnapshot(), true, 'uncertainty must not disable actions')
  await failedCheck

  reachable = true
  await h.store.verify()
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(h.store.getRecoverySnapshot(), 1)
  assert.equal(notifications, 2)

  h.store.reportReachable()
  assert.equal(h.store.getRecoverySnapshot(), 1)
  assert.equal(notifications, 2, 'settled responses must not repeat recovery')
  stop()
})

test('returning to a stale-false browser flag confirms recovery promptly', async () => {
  let reachable = false
  const h = harness(async () => {
    if (!reachable) throw new TypeError('offline')
    return { ok: true }
  })
  h.navigatorTarget.onLine = false
  const stop = h.store.subscribe(() => {})
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), false)

  reachable = true
  h.documentTarget.emit('visibilitychange')
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), false, 'one success still rejects the device anomaly')
  h.timers.runTimeout(AMBIGUOUS_VERDICT_CONFIRM_MS)
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true, 'the prompt second success completes recovery')
  stop()
})

test('the hook and API client consume the shared store contract', () => {
  const hook = readFileSync(new URL('../../hooks/useOnlineStatus.js', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
  assert.match(hook, /useSyncExternalStore\(subscribeOnline, getOnlineSnapshot/)
  assert.match(hook, /getReachabilityPhaseSnapshot/)
  assert.match(hook, /useRecoveryGeneration[\s\S]*?getRecoverySnapshot/)
  assert.doesNotMatch(hook, /fetch\(|setInterval\(/)
  assert.match(client, /void verifyConnectivity\(\)/)
})

test('visible chats subscribe their runtime owner to shared recovery', () => {
  const chatView = readFileSync(
    new URL('../../components/ChatView/ChatView.jsx', import.meta.url),
    'utf8',
  )
  assert.match(
    chatView,
    /reconcileRuntimeState\(\)\.then\(runtime => \{[\s\S]*?subscribeRecovery\([\s\S]*?getRecoverySnapshot\(\)[\s\S]*?run\(\)/,
    'every visible pane rechecks durable runtime after shared reachability recovers',
  )
})
