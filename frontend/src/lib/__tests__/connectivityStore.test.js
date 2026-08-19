import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  createConnectivityStore,
  FAILURE_GRACE_MS,
  ReachabilityPhase,
  RECOVERY_RETRY_MIN_MS,
  reduceReachability,
} from '../connectivityStore.js'

function eventTarget(extra = {}) {
  const listeners = new Map()
  return {
    ...extra,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set())
      listeners.get(type).add(listener)
    },
    removeEventListener(type, listener) { listeners.get(type)?.delete(listener) },
    emit(type) { for (const listener of listeners.get(type) || []) listener() },
    listenerCount(type) { return listeners.get(type)?.size || 0 },
  }
}

function fakeTimers() {
  let nextId = 1
  const timeouts = new Map()
  return {
    setTimeoutFn(callback, delay) {
      const id = nextId++
      timeouts.set(id, { callback, delay })
      return id
    },
    clearTimeoutFn(id) { timeouts.delete(id) },
    runTimeout(delay) {
      const found = [...timeouts].find(([, task]) => task.delay === delay)
      assert.ok(found, `expected a ${delay}ms timeout`)
      const [id, task] = found
      timeouts.delete(id)
      task.callback()
    },
    countDelay(delay) {
      return [...timeouts.values()].filter(task => task.delay === delay).length
    },
    timeoutCount: () => timeouts.size,
  }
}

async function flush() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve()
}

function harness(fetchImpl, { navigatorOnline = true } = {}) {
  const windowTarget = eventTarget()
  const documentTarget = eventTarget({ visibilityState: 'visible' })
  const navigatorTarget = { onLine: navigatorOnline }
  const timers = fakeTimers()
  const store = createConnectivityStore({
    windowTarget, documentTarget, navigatorTarget, fetchImpl, ...timers,
  })
  return { store, windowTarget, documentTarget, navigatorTarget, timers }
}

test('the pure core exposes Online, Checking, and Offline without presentation policy', () => {
  let state = { phase: ReachabilityPhase.ONLINE, staleOfflineSuccesses: 0, recoveryGeneration: 0 }
  state = reduceReachability(state, { type: 'failed' })
  assert.equal(state.phase, ReachabilityPhase.CHECKING)
  state = reduceReachability(state, { type: 'deadline' })
  assert.equal(state.phase, ReachabilityPhase.OFFLINE)
  state = reduceReachability(state, { type: 'reachable', strong: true })
  assert.deepEqual(state, {
    phase: ReachabilityPhase.ONLINE,
    staleOfflineSuccesses: 0,
    recoveryGeneration: 1,
  })
})

test('any HTTP response proves reachability, including a 500 response', async () => {
  const h = harness(async () => ({ ok: false, status: 500 }))
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.store.getState().phase, ReachabilityPhase.ONLINE)
  assert.equal(h.store.getSnapshot(), true)
  stop()
})

test('one continuous failure deadline owns demotion and foreground storms cannot extend it', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.store.getState().phase, ReachabilityPhase.CHECKING)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.CHECKING)
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1)

  h.windowTarget.emit('focus')
  h.windowTarget.emit('pageshow')
  h.documentTarget.emit('visibilitychange')
  await flush()
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1)

  h.timers.runTimeout(FAILURE_GRACE_MS)
  assert.equal(h.store.getState().phase, ReachabilityPhase.OFFLINE)
  assert.equal(h.store.getSnapshot(), false)
  stop()
})

test('recovery retries use one scheduler and healthy operation has no interval', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.timers.countDelay(RECOVERY_RETRY_MIN_MS), 1)
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1)
  stop()
  assert.equal(h.timers.timeoutCount(), 0)
})

test('strong live evidence repairs uncertainty and emits one recovery generation', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flush()
  assert.equal(notifications, 1, 'Checking is visible without disabling online actions')
  h.store.reportReachable()
  assert.equal(h.store.getState().phase, ReachabilityPhase.ONLINE)
  assert.equal(h.store.getRecoverySnapshot(), 1)
  assert.equal(notifications, 2)
  assert.equal(h.timers.timeoutCount(), 0)
  stop()
})

test('newer live evidence outranks an older failed probe', async () => {
  let settle
  const h = harness(() => new Promise((_, reject) => { settle = reject }))
  const stop = h.store.subscribe(() => {})
  h.store.reportReachable()
  settle(new TypeError('old failure'))
  await flush()
  assert.equal(h.store.getState().phase, ReachabilityPhase.ONLINE)
  assert.equal(h.store.getSnapshot(), true)
  stop()
})

test('cold stale-false startup remains Offline until two ordinary successes', async () => {
  const h = harness(async () => ({ status: 204 }), { navigatorOnline: false })
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.store.getSnapshot(), false)
  h.timers.runTimeout(RECOVERY_RETRY_MIN_MS)
  await flush()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getRecoverySnapshot(), 1)
  stop()
})

test('hidden tabs pause recovery and visibility requests one coalesced check', async () => {
  const h = harness(async () => { throw new TypeError('offline') })
  const stop = h.store.subscribe(() => {})
  await flush()
  h.documentTarget.visibilityState = 'hidden'
  h.documentTarget.emit('visibilitychange')
  assert.equal(h.timers.countDelay(RECOVERY_RETRY_MIN_MS), 0)
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1, 'failure history is not reset')
  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  await flush()
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1)
  stop()
})

test('verification without subscribers is bounded and creates no lifecycle owner', async () => {
  const h = harness(async () => ({ status: 401 }))
  assert.equal(await h.store.verify(), true)
  assert.equal(h.windowTarget.listenerCount('focus'), 0)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(h.timers.timeoutCount(), 0)
})

test('the hook and API client consume the shared store contract', () => {
  const hook = readFileSync(new URL('../../hooks/useOnlineStatus.js', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
  assert.match(hook, /useSyncExternalStore\(subscribeOnline, getOnlineSnapshot/)
  assert.match(hook, /getReachabilityPhaseSnapshot/)
  assert.doesNotMatch(hook, /fetch\(|setInterval\(/)
  assert.match(client, /void verifyConnectivity\(\)/)
  assert.match(client, /reportNetworkReachable\(\)/)
})

test('both durable streams feed recovery and an exhausted chat observes it', () => {
  const chat = readFileSync(
    new URL('../../components/ChatView/useStreamConnection.js', import.meta.url),
    'utf8',
  )
  const chatView = readFileSync(
    new URL('../../components/ChatView/ChatView.jsx', import.meta.url),
    'utf8',
  )
  const system = readFileSync(
    new URL('../../hooks/useSystemEventStream.js', import.meta.url),
    'utf8',
  )
  assert.match(chat, /const res = await fetch\([\s\S]*?reportNetworkReachable\(\)/)
  assert.match(chat, /subscribeRecovery\([\s\S]*?shouldReconnectExhaustedStream\(/)
  assert.match(
    chatView,
    /const run = \(\) => \{[\s\S]*?reconcileRuntimeState\(\)[\s\S]*?subscribeRecovery\([\s\S]*?getRecoverySnapshot\(\)[\s\S]*?run\(\)/,
    'every visible pane rechecks durable runtime after shared reachability recovers',
  )
  assert.match(chat, /catch \(err\) \{[\s\S]*?void verifyConnectivity\(\)/)
  assert.match(
    system,
    /if \(!cancelled\) \{[\s\S]*?void verifyConnectivity\(\)[\s\S]*?setTimeout/,
    'an unexpected system-stream close must enter shared reachability recovery',
  )
  assert.match(system, /const res = await fetch\([\s\S]*?reportNetworkReachable\(\)/)
})
