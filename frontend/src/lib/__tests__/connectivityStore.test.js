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

function readiness(ready = true, bootId = 'boot-a') {
  return { ok: ready, status: ready ? 200 : 503,
    json: async () => ({ ready, boot_id: bootId }) }
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
  state = reduceReachability(state, { type: 'checking' })
  assert.equal(state.phase, ReachabilityPhase.CHECKING)
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

test('all subscribers share one monitor and the last unsubscribe releases it', async () => {
  const h = harness(async () => readiness())
  const stopA = h.store.subscribe(() => {})
  const stopB = h.store.subscribe(() => {})
  await flush()

  assert.equal(h.windowTarget.listenerCount('online'), 1)
  assert.equal(h.windowTarget.listenerCount('offline'), 1)
  assert.equal(h.windowTarget.listenerCount('focus'), 1)
  assert.equal(h.windowTarget.listenerCount('pageshow'), 1)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 1)

  stopA()
  assert.equal(h.windowTarget.listenerCount('online'), 1)
  stopB()
  assert.equal(h.windowTarget.listenerCount('online'), 0)
  assert.equal(h.windowTarget.listenerCount('offline'), 0)
  assert.equal(h.windowTarget.listenerCount('focus'), 0)
  assert.equal(h.windowTarget.listenerCount('pageshow'), 0)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(h.timers.timeoutCount(), 0)
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

test('strong transport evidence restores browsing but recovery waits for service readiness', async () => {
  let available = false
  const h = harness(async () => {
    if (!available) throw new TypeError('offline')
    return readiness()
  })
  const stop = h.store.subscribe(() => {})
  await flush()
  available = true
  h.store.reportReachable()
  assert.equal(h.store.getState().phase, ReachabilityPhase.ONLINE)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  assert.equal(h.store.getRecoverySnapshot(), 0)
  await flush()
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  assert.equal(h.store.getRecoverySnapshot(), 1)
  assert.equal(h.timers.timeoutCount(), 0)
  h.store.reportReachable()
  assert.equal(h.store.getRecoverySnapshot(), 1, 'settled responses do not repeat recovery')
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
  const h = harness(async () => readiness(), { navigatorOnline: false })
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
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 0, 'background time cannot confirm an outage')
  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  await flush()
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 1)
  stop()
})

test('hidden verification defers without publishing a false Checking state', async () => {
  let probes = 0
  const h = harness(async () => { probes += 1; return { status: 204 } })
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(probes, 1)

  h.documentTarget.visibilityState = 'hidden'
  h.documentTarget.emit('visibilitychange')
  assert.equal(await h.store.verify(), true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(probes, 1, 'the hidden transport is not probed')

  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  await flush()
  assert.equal(probes, 2, 'foreground return owns one fresh probe')
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  stop()
})

test('healthy verification keeps the last reachable verdict while its probe settles', async () => {
  let resolveProbe
  let probes = 0
  const h = harness(() => {
    probes += 1
    if (probes === 1) return Promise.resolve(readiness())
    return new Promise(resolve => { resolveProbe = resolve })
  })
  let notifications = 0
  const stop = h.store.subscribe(() => { notifications += 1 })
  await flush()
  notifications = 0 // Initial readiness is separate from transport re-verification.

  const verification = h.store.verify()
  assert.equal(probes, 2)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE,
    'a transport reconnect is not a server outage verdict')
  assert.equal(notifications, 0, 'the shell status dot must not flash before verification')

  resolveProbe(readiness())
  assert.equal(await verification, true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(notifications, 0)
  stop()
})

test('foreground recovery does not wait for or accept a suspended probe', async () => {
  let probes = 0
  let rejectSuspended
  const h = harness(() => {
    probes += 1
    if (probes === 2) {
      return new Promise((_, reject) => { rejectSuspended = reject })
    }
    return Promise.resolve({ status: 204 })
  })
  const stop = h.store.subscribe(() => {})
  await flush()

  const suspended = h.store.verify()
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(probes, 2)

  h.documentTarget.visibilityState = 'hidden'
  h.documentTarget.emit('visibilitychange')
  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  await flush()

  assert.equal(probes, 3)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)

  rejectSuspended(new TypeError('suspended transport failed late'))
  await suspended
  await flush()
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE,
    'the detached failure cannot overwrite foreground truth')
  stop()
})

test('verification without subscribers is bounded and creates no lifecycle owner', async () => {
  const h = harness(async () => ({ status: 401 }))
  assert.equal(await h.store.verify(), true)
  assert.equal(h.windowTarget.listenerCount('focus'), 0)
  assert.equal(h.documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(h.timers.timeoutCount(), 0)

  const failed = harness(async () => { throw new TypeError('offline') })
  assert.equal(await failed.store.verify(), false)
  assert.equal(failed.store.getPhaseSnapshot(), ReachabilityPhase.CHECKING)
  assert.equal(failed.windowTarget.listenerCount('focus'), 0)
  assert.equal(failed.documentTarget.listenerCount('visibilitychange'), 0)
  assert.equal(failed.timers.timeoutCount(), 0)
})

test('the hook and API client consume the shared store contract', () => {
  const hook = readFileSync(new URL('../../hooks/useOnlineStatus.js', import.meta.url), 'utf8')
  const client = readFileSync(new URL('../../api/client.js', import.meta.url), 'utf8')
  assert.match(hook, /useSyncExternalStore\(subscribeOnline, getOnlineSnapshot/)
  assert.match(hook, /getReachabilityPhaseSnapshot/)
  assert.match(hook, /useRecoveryGeneration[\s\S]*?getRecoverySnapshot/)
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
  assert.match(
    chatView,
    /const run = \(\{ recovery = false \} = \{\}\) => \{[\s\S]*?reconcileRuntimeState\(\)[\s\S]*?subscribeRecovery\([\s\S]*?getRecoverySnapshot\(\)[\s\S]*?run\(\{ recovery: true \}\)/,
    'every mounted pane rechecks durable runtime after shared reachability recovers',
  )
  assert.match(chat, /catch \(err\) \{[\s\S]*?void verifyConnectivity\(\)/)
  assert.match(
    system,
    /if \(!stopped\) \{[\s\S]*?void verifyConnectivity\(\)[\s\S]*?scheduleRetry\(\)/,
    'an unexpected system-stream close must enter shared reachability recovery',
  )
  assert.match(system, /const res = await fetch\([\s\S]*?reportNetworkReachable\(\)/)
})

test('checking and reachable service failure queue sends without calling the device offline', async () => {
  let response = readiness()
  const h = harness(async () => response)
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  response = readiness(false)
  h.windowTarget.emit('offline')
  assert.equal(h.store.getDeliveryReadySnapshot(), false, 'observed interruption suspends delivery synchronously')
  await flush()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getPhaseSnapshot(), ReachabilityPhase.ONLINE)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  assert.equal(h.timers.countDelay(FAILURE_GRACE_MS), 0)
  stop()
})

test('browser offline suspends delivery before its probe settles', async () => {
  let response = readiness()
  const h = harness(async () => response)
  const stop = h.store.subscribe(() => {})
  await flush()
  response = new Promise(() => {})
  h.navigatorTarget.onLine = false
  h.windowTarget.emit('offline')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  stop()
})

test('an old process answering readiness never releases a planned restart', async () => {
  let response = readiness()
  const h = harness(async () => response)
  const stop = h.store.subscribe(() => {})
  await flush()
  h.store.setRestartPending('boot-a')
  await flush()
  assert.equal(h.store.getRestartPendingSnapshot(), true)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  response = readiness(false, 'boot-b')
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), true, 'a later but unready boot is insufficient')
  response = readiness(true, 'boot-b')
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), false)
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  assert.equal(h.timers.timeoutCount(), 0, 'healthy operation has no expiry/retry timer')
  stop()
})

test('an in-flight pre-restart response cannot approve delivery after restart was observed', async () => {
  let settle
  let response = readiness()
  const h = harness(() => response)
  const stop = h.store.subscribe(() => {})
  await flush()
  response = new Promise(resolve => { settle = resolve })
  const check = h.store.verify()
  h.store.setRestartPending('boot-a')
  response = new Promise(() => {})
  settle(readiness(true, 'boot-b'))
  await check
  assert.equal(h.store.getRestartPendingSnapshot(), true)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  stop()
})

test('cold startup and concurrent verification never guess readiness from navigator', async () => {
  let settle
  const h = harness(() => new Promise(resolve => { settle = resolve }))
  assert.equal(h.store.getSnapshot(), true, 'browsing can mount immediately')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  const first = h.store.verify()
  const second = h.store.verify()
  assert.equal(first, second, 'one bounded probe owns concurrent verification')
  settle(readiness())
  await first
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  assert.equal(h.store.getRecoverySnapshot(), 1)
})

test('a proxy success without the application readiness body never permits delivery', async () => {
  const h = harness(async () => new Response('<html>Proxy ready</html>'))
  const stop = h.store.subscribe(() => {})
  await flush()
  assert.equal(h.store.getSnapshot(), true)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  stop()
})


test('returning from background rechecks readiness before releasing queued delivery', async () => {
  let resolveProbe
  let response = readiness()
  const h = harness(() => response)
  const stop = h.store.subscribe(() => {})
  await flush()
  h.documentTarget.visibilityState = 'hidden'
  h.documentTarget.emit('visibilitychange')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  assert.equal(h.store.getSnapshot(), true, 'suspension is not an Offline verdict')
  response = new Promise(resolve => { resolveProbe = resolve })
  h.documentTarget.visibilityState = 'visible'
  h.documentTarget.emit('visibilitychange')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  resolveProbe(readiness())
  await flush()
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  stop()
})

test('frontend-first upgrade uses only readiness and waits for the modern backend', async () => {
  let response = { ready: true }
  const urls = []
  const h = harness(async url => { urls.push(url); return Response.json(response) })
  await h.store.verify()
  assert.equal(h.store.getDeliveryReadySnapshot(), true,
    'ordinary delivery still works before backend activation')

  h.store.setRestartPending()
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), true,
    'the still-answering legacy worker cannot release its restart')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)

  response = { ready: false, boot_id: 'upgraded-worker' }
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), true,
    'upgraded identity without readiness is insufficient')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  response = { ready: true, boot_id: 'upgraded-worker' }
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), false)
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  assert.deepEqual(urls, Array(4).fill('/api/ready'),
    'readiness and identity never come from separate worker responses')
})

test('a cold legacy restart needs a readiness capability transition, not time or a guessed identity', async () => {
  let modern = false
  const h = harness(async () => modern
    ? readiness(true, 'upgraded-worker') : Response.json({ ready: true }))
  // The old system stream can deliver this before the initial readiness probe.
  h.store.setRestartPending()
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), true)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), true,
    'another legacy response cannot prove a worker change')
  modern = true
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), false,
    'the legacy event producer could not have supplied boot-identifying readiness')
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
})

test('malformed modern restart identities cannot borrow prior observations or claim the legacy exception', async () => {
  for (const sourceBootId of [null, '', ' ', 7]) {
    let response = readiness(true, 'known-worker')
    const h = harness(async () => response)
    await h.store.verify()
    response = readiness(true, 'another-worker')
    h.store.setRestartPending(sourceBootId)
    await h.store.verify()
    assert.equal(h.store.getRestartPendingSnapshot(), true)
    assert.equal(h.store.getDeliveryReadySnapshot(), false)
  }
})

test('a pre-event readiness response cannot certify a later legacy restart', async () => {
  let resolveOldProbe
  let response = new Promise(resolve => { resolveOldProbe = resolve })
  const h = harness(() => response)
  const oldProbe = h.store.verify()
  h.store.setRestartPending()
  resolveOldProbe(readiness(true, 'some-worker'))
  await oldProbe
  assert.equal(h.store.getRestartPendingSnapshot(), true,
    'the legacy transition still requires evidence started after the event')
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  response = readiness(true, 'some-worker')
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), false)
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
})

test('proxy and malformed readiness bodies never authorize delivery or create another probe path', async () => {
  for (const body of [{}, { ready: 'true' }, { ready: true, boot_id: 7 },
    { ready: true, boot_id: '' }, { ready: true, boot_id: ' ' }]) {
    const urls = []
    const h = harness(async url => {
      urls.push(url)
      return Response.json(body)
    })
    await h.store.verify()
    assert.equal(h.store.getSnapshot(), true)
    assert.equal(h.store.getDeliveryReadySnapshot(), false)
    assert.deepEqual(urls, ['/api/ready'])
  }
})

test('cold modern startup without a restart event requires coherent ready evidence from that worker', async () => {
  let response = readiness(false, 'cold-worker')
  const urls = []
  const h = harness(async url => { urls.push(url); return response })
  await h.store.verify()
  assert.equal(h.store.getRestartPendingSnapshot(), false)
  assert.equal(h.store.getDeliveryReadySnapshot(), false)
  response = readiness(true, 'cold-worker')
  await h.store.verify()
  assert.equal(h.store.getDeliveryReadySnapshot(), true)
  assert.deepEqual(urls, ['/api/ready', '/api/ready'])
})
