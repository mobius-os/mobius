import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'


function target(extra = {}) {
  return Object.assign(new EventTarget(), extra)
}


function installBrowser() {
  globalThis.window = target({ location: { reload() {} } })
  globalThis.document = target({ visibilityState: 'visible' })
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { onLine: true },
  })
  globalThis.localStorage = {
    getItem(key) { return key === 'token' ? 'owner-token' : null },
    removeItem() {},
  }
  globalThis.sessionStorage = { setItem() {} }
}


async function flushMicrotasks() {
  for (let index = 0; index < 10; index += 1) await Promise.resolve()
}


test('reconnect reconciliation settles before buffered system events are applied', async () => {
  installBrowser()

  let releaseReconciliation
  const reconciliation = new Promise(resolve => { releaseReconciliation = resolve })
  const order = []
  let firstRead = true
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader() {
        order.push('reader-opened')
        return {
          releaseLock() {},
          read() {
            if (firstRead) {
              firstRead = false
              return Promise.resolve({
                done: false,
                value: new TextEncoder().encode(
                  'data: {"type":"chat_run_started","chat_id":"new-run"}\n\n',
                ),
              })
            }
            return new Promise(() => {})
          },
        }
      },
    },
  })

  const { default: useSystemEventStream } = await import(
    '../../../hooks/useSystemEventStream.js'
  )
  const hook = renderHook(
    useSystemEventStream,
    event => order.push(`event:${event.chat_id}`),
    {
      onOpen: () => {
        order.push('reconciliation-started')
        return reconciliation
      },
    },
  )
  await flushMicrotasks()

  assert.deepEqual(order, ['reconciliation-started'])

  releaseReconciliation()
  await flushMicrotasks()

  assert.deepEqual(order, [
    'reconciliation-started',
    'reader-opened',
    'event:new-run',
  ])

  hook.unmount()
})

test('unmount during reconnect reconciliation never acquires the stream reader', async () => {
  installBrowser()
  let releaseCancelledReconciliation
  const cancelledReconciliation = new Promise(resolve => {
    releaseCancelledReconciliation = resolve
  })
  const cancelledOrder = []
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    body: {
      getReader() {
        cancelledOrder.push('reader-opened')
        return { read: () => new Promise(() => {}) }
      },
    },
  })
  const { default: useSystemEventStream } = await import(
    '../../../hooks/useSystemEventStream.js'
  )
  const cancelledHook = renderHook(
    useSystemEventStream,
    () => {},
    {
      onOpen: () => {
        cancelledOrder.push('reconciliation-started')
        return cancelledReconciliation
      },
    },
  )
  await flushMicrotasks()
  cancelledHook.unmount()
  releaseCancelledReconciliation()
  await flushMicrotasks()

  assert.deepEqual(cancelledOrder, ['reconciliation-started'])
})


// A suspended socket can stay pending without an error. The fake clock advances
// browser lifecycle time, not an agent turn, and each fixture owns its reader.
async function connectionHarness(t, { onOpen, ignoreAbort = false, stallHeaders = false } = {}) {
  installBrowser()
  let now = 1000, nextTimer = 1
  const timers = new Map()
  t.mock.method(Date, 'now', () => now)
  t.mock.method(globalThis, 'setTimeout', (fn, delay = 0) => {
    const id = nextTimer++
    timers.set(id, { at: now + delay, fn })
    return id
  })
  t.mock.method(globalThis, 'clearTimeout', id => timers.delete(id))
  const connections = [], events = [], opens = []
  t.mock.method(globalThis, 'fetch', async (url, { signal } = {}) => {
    if (!String(url).endsWith('/events/system')) return { ok: true, status: 200 }
    const connection = { signal, reads: 0, released: false, pending: null, queue: [] }
    connections.push(connection)
    const send = value => {
      if (connection.pending) {
        connection.pending.resolve(value)
        connection.pending = null
      } else connection.queue.push(value)
    }
    connection.bytes = text => send({ done: false, value: new TextEncoder().encode(text) })
    connection.end = () => send({ done: true })
    connection.bytes('data: {"type":"system_stream_open"}\n\n')
    signal.addEventListener('abort', () => {
      if (!ignoreAbort) connection.pending?.reject(signal.reason)
    }, { once: true })
    if (stallHeaders) return new Promise(() => {})
    return { ok: true, status: 200, body: { getReader() {
      connection.reads++
      return {
        read: () => connection.queue.length
          ? Promise.resolve(connection.queue.shift())
          : new Promise((resolve, reject) => { connection.pending = { resolve, reject } }),
        releaseLock: () => { connection.released = true },
      }
    } } }
  })
  const { default: useSystemEventStream } = await import('../../../hooks/useSystemEventStream.js')
  const hook = renderHook(useSystemEventStream, event => events.push(event), {
    onOpen: options => { opens.push(options); return onOpen?.(options) },
  })
  async function flush() { for (let i = 0; i < 40; i++) await Promise.resolve() }
  async function tick(ms) {
    const end = now + ms
    for (;;) {
      const due = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      now = due[1].at
      timers.delete(due[0]); due[1].fn(); await flush()
    }
    now = end; await flush()
  }
  function visibility(state) {
    document.visibilityState = state
    document.dispatchEvent(new Event('visibilitychange'))
  }
  function wakeBurst() {
    visibility('visible')
    for (const type of ['focus', 'pageshow', 'online']) window.dispatchEvent(new Event(type))
  }
  t.after(async () => { hook.unmount(); await flush() })
  await flush()
  return { hook, connections, events, opens, tick, flush, visibility, wakeBurst, timers }
}

test('long absence repairs a silently stalled socket once and fresh truth clears the answered question', async t => {
  let serverQuestion = 'q1', drawerQuestion
  const h = await connectionHarness(t, { onOpen: () => { drawerQuestion = serverQuestion } })
  assert.equal(drawerQuestion, 'q1')
  h.visibility('hidden')
  await h.tick(120_000)
  assert.equal(h.connections.length, 1, 'hidden pages must not churn connections')
  serverQuestion = null // The answer is committed while the old socket is silent.
  h.wakeBurst(); await h.flush()
  assert.equal(h.connections.length, 2)
  assert.equal(h.connections[0].signal.aborted, true)
  assert.equal(h.connections[1].signal.aborted, false)
  assert.equal(drawerQuestion, null)
  h.connections[1].bytes('data: {"type":"chat_owner_input_changed","questionId":"q2"}\n\n')
  await h.flush()
  assert.equal(h.events[0].questionId, 'q2', 'the replacement continues delivering live updates')
})

test('healthy quick switches keep their socket but a silently dead one still expires', async t => {
  const h = await connectionHarness(t)
  h.visibility('hidden'); await h.tick(1000); h.wakeBurst(); await h.flush()
  assert.equal(h.connections.length, 1)
  await h.tick(69_000)
  assert.equal(h.connections[0].signal.aborted, true)
  await h.tick(1000)
  assert.equal(h.connections.length, 2)
})

test('keepalive comments renew connection health without forwarding fake events', async t => {
  const h = await connectionHarness(t)
  for (let i = 0; i < 4; i++) {
    await h.tick(30_000)
    h.connections[0].bytes(': keepalive\n\n'); await h.flush()
  }
  assert.equal(h.connections.length, 1)
  assert.deepEqual(h.events, [])
})

test('headers that never arrive have a deadline and a cancellable retry', async t => {
  const h = await connectionHarness(t, { stallHeaders: true })
  await h.tick(30_000)
  assert.equal(h.connections[0].signal.aborted, true)
  await h.tick(1000)
  assert.equal(h.connections.length, 2)
  h.hook.unmount(); await h.tick(120_000)
  assert.equal(h.connections.length, 2)
  assert.equal(h.timers.size, 0)
})

test('a stalled reconciliation is cancelled and cannot consume old events after replacement', async t => {
  let release
  let count = 0
  const h = await connectionHarness(t, { onOpen: () => ++count === 1 ? new Promise(resolve => { release = resolve }) : undefined })
  await h.tick(31_000)
  assert.equal(h.connections.length, 2)
  assert.equal(h.opens[0].signal.aborted, true)
  release(); await h.flush()
  assert.equal(h.connections[0].reads, 0)
  assert.equal(h.connections[1].reads, 1)
})

test('failed fresh-state reconciliation retries rather than accepting stale buffered events', async t => {
  let count = 0
  const h = await connectionHarness(t, { onOpen: () => { if (++count === 1) throw new Error('fresh list unavailable') } })
  assert.equal(h.connections[0].signal.aborted, true)
  assert.equal(h.connections[0].reads, 0)
  await h.tick(1000)
  assert.equal(h.connections[1].reads, 1)
})

test('an old read resolving after abort cannot deliver events or retire the replacement', async t => {
  const h = await connectionHarness(t, { ignoreAbort: true })
  h.visibility('hidden'); await h.tick(20_000); h.wakeBurst(); await h.flush()
  h.connections[0].bytes('data: {"type":"old-event"}\n\n'); await h.flush()
  assert.deepEqual(h.events, [])
  assert.equal(h.connections[1].signal.aborted, false)
  await h.tick(1000)
  assert.equal(h.connections.length, 2)
})

test('clean EOF feeds recovery and reconnects; unmount removes wake listeners and timers', async t => {
  const h = await connectionHarness(t)
  h.connections[0].end(); await h.flush(); await h.tick(1000)
  assert.equal(h.connections.length, 2)
  h.hook.unmount(); h.wakeBurst(); await h.tick(120_000)
  assert.equal(h.connections.length, 2)
  assert.equal(h.timers.size, 0)
})

test('focus and restored-page signals repair expired health even without visibilitychange', async t => {
  const h = await connectionHarness(t)
  // A frozen page does not execute timers while wall-clock time passes.
  t.mock.method(Date, 'now', () => 200_000)
  window.dispatchEvent(new Event('pageshow'))
  window.dispatchEvent(new Event('focus'))
  await h.flush()
  assert.equal(h.connections.length, 2)
  assert.equal(h.connections[0].signal.aborted, true)
})
