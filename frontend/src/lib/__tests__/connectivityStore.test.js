import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  AMBIGUOUS_FAILURE_CONFIRM_MS,
  createConnectivityStore,
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
  return { store, windowTarget, documentTarget, timers }
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

test('a stale-online failure is confirmed promptly and published once', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()

  assert.equal(h.store.getSnapshot(), true)
  h.timers.runTimeout(AMBIGUOUS_FAILURE_CONFIRM_MS)
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
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
})

test('a live mutation response repairs a stale offline verdict immediately', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flushMicrotasks()
  h.timers.runTimeout(AMBIGUOUS_FAILURE_CONFIRM_MS)
  await flushMicrotasks()
  assert.equal(h.store.getSnapshot(), false)

  h.store.reportReachable()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(notifications, 2)
  stop()
})

test('the hook and API client consume the shared store contract', () => {
  const hook = readFileSync(new URL('../../hooks/useOnlineStatus.js', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
  assert.match(hook, /useSyncExternalStore\(subscribeOnline, getOnlineSnapshot/)
  assert.doesNotMatch(hook, /fetch\(|setInterval\(/)
  assert.match(client, /void verifyConnectivity\(\)/)
})
