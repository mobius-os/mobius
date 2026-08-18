import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AMBIGUOUS_VERDICT_CONFIRM_MS,
  createConnectivityStore,
  RESUME_NETWORK_GRACE_MS,
  RESUME_RETRY_MS,
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
  assert.equal(h.windowTarget.listenerCount('focus'), 1)
  assert.equal(h.windowTarget.listenerCount('pageshow'), 1)
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

test('a stale-online failure is confirmed promptly and published once', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  h.timers.runTimeout(AMBIGUOUS_VERDICT_CONFIRM_MS)
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), false)
  assert.equal(notifications, 1)
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
  assert.equal(h.windowTarget.listenerCount('focus'), 0)
  assert.equal(h.windowTarget.listenerCount('pageshow'), 0)
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
  assert.equal(notifications, 2)
  stop()
})

test('a live mutation response outranks an older in-flight failed probe', async () => {
  let settleProbe
  const h = harness(() => new Promise((resolve) => { settleProbe = resolve }))
  h.navigatorTarget.onLine = false
  const stop = h.store.subscribe(() => {})

  h.store.reportReachable()
  settleProbe({ ok: false })
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.timers.timeoutCount(), 0)
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

test('resume preserves the confirmed online verdict while the network wakes', async () => {
  let reachable = true
  const h = harness(async () => {
    if (!reachable) throw new TypeError('network still waking')
    return { ok: true }
  })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()

  reachable = false
  h.windowTarget.emit('focus')
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  assert.equal(notifications, 0)

  h.timers.runTimeout(RESUME_RETRY_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), true, 'an early retry still cannot flash Offline')

  reachable = true
  h.timers.runTimeout(RESUME_RETRY_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(notifications, 0, 'a normal wake remains visually quiet')
  stop()
})

test('resume publishes Offline when reachability stays lost beyond the grace', async () => {
  let reachable = true
  const h = harness(async () => {
    if (!reachable) throw new TypeError('offline')
    return { ok: true }
  })
  const stop = h.store.subscribe(() => {})
  await flushMicrotasks()

  h.documentTarget.visibilityState = 'hidden'
  h.documentTarget.emit('visibilitychange')
  reachable = false
  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  await flushMicrotasks()

  h.timers.runTimeout(RESUME_NETWORK_GRACE_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), true, 'ordinary failure hysteresis still applies')

  h.timers.runTimeout(AMBIGUOUS_VERDICT_CONFIRM_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), false)
  stop()
})

test('the hook and API client consume the shared store contract', () => {
  const hook = readFileSync(new URL('../../hooks/useOnlineStatus.js', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
  assert.match(hook, /useSyncExternalStore\(subscribeOnline, getOnlineSnapshot/)
  assert.doesNotMatch(hook, /fetch\(|setInterval\(/)
  assert.match(client, /void verifyConnectivity\(\)/)
})
