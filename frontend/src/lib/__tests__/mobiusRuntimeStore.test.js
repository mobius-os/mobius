/**
 * Deterministic unit tests for the STATEFUL offline core of the mini-app
 * runtime (frontend/public/mobius-runtime.js → makeStorage):
 *
 *   - withPathLock per-path serialization (concurrent writes don't interleave),
 *   - the read-through cache (write/online-read → offline-read serves the mirror,
 *     a tombstone after an offline delete returns null),
 *   - the outbox: FIFO drain order across paths, path-coalescing (LWW), a
 *     transient failure stops-and-preserves order, a 404-DELETE counts as synced,
 *   - subscribe(): fires the initial value then on every set/remove, with the
 *     `delivered` tie-break so a slow initial get() never lands after a newer set,
 *   - drainInner's poison-op dead-letter + cache reconcile (a 4xx write is dropped
 *     AND the subscriber reconciles to the authoritative server value),
 *   - logout: deleting the shared mobius-outbox DB purges the cache mirror too.
 *
 * The PURE read-your-writes overlay (overlayPending) is covered separately in
 * mobiusRuntime.test.js. Everything here drives the real async state machine
 * through fake-indexeddb + a controlled fetch/online flag (mobiusRuntimeHarness),
 * so the paths the card (079) flagged as "e2e-only, flaky" are pinned headless.
 *
 * Run with:
 *   cd frontend && npm test            # part of test:lib (globs src/lib)
 *   cd frontend && node --test src/lib/__tests__/mobiusRuntimeStore.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import { freshEnv, recordSubscription, waitFor } from './mobiusRuntimeHarness.mjs'

// makeStorage is imported lazily inside each test AFTER freshEnv() installs the
// browser globals it reads at construction time. A top-level import would run
// before the globals exist.
function appToken(appId, nonce = null, rev = '1') {
  const payload = Buffer.from(JSON.stringify({
    scope: 'app', app_id: appId, ...(nonce ? { app_nonce: nonce } : {}), rev,
  })).toString('base64url')
  return `header.${payload}.signature`
}

async function newStorage(appId = '1', { appInstanceId = null, getToken } = {}) {
  const { makeStorage } = await import('../../../public/mobius-runtime.js')
  return makeStorage({
    appId,
    appInstanceId,
    getToken: getToken || (async () => appToken(appId, appInstanceId)),
  })
}

async function runtimeExports() {
  return import('../../../public/mobius-runtime.js')
}

test('an opaque sandbox without IndexedDB still reads and writes online', async () => {
  const { server } = freshEnv()
  globalThis.indexedDB = {
    open() {
      const err = new Error('IndexedDB is denied in an opaque origin')
      err.name = 'SecurityError'
      throw err
    },
  }
  const s = await newStorage()
  server.seed('saved.json', { survives: true })

  assert.deepEqual(await s.get('saved.json'), { survives: true })
  assert.deepEqual((await s.list('')).map((entry) => entry.name), ['saved.json'])
  assert.deepEqual(await s.set('new.json', { writes: true }), { synced: true })
  assert.deepEqual(server.serverValue('new.json'), { writes: true })
  assert.equal(await s.pendingCount(), 0)

  server.setOnline(false)
  assert.equal(await s.get('saved.json'), null)
  await assert.rejects(s.set('offline.json', { no: 'phantom queue' }), /offline saving is unavailable/)
})

test('list can batch JSON content in one bounded server request', async () => {
  const { server } = freshEnv()
  globalThis.indexedDB = {
    open() { throw new DOMException('denied', 'SecurityError') },
  }
  const s = await newStorage()
  server.seed('records/a.json', { id: 'a' })
  server.seed('records/b.json', { id: 'b' })
  server.seed('records/readme.txt', 'hello', 'text', 'text/plain')

  const entries = await s.list('records', { includeContent: true })
  const byName = new Map(entries.map((entry) => [entry.name, entry]))
  assert.deepEqual(byName.get('a.json').content, { id: 'a' })
  assert.deepEqual(byName.get('b.json').content, { id: 'b' })
  assert.equal(Object.hasOwn(byName.get('readme.txt'), 'content'), false)
  assert.equal(
    server.log.filter((request) => request.url.includes('/apps-list/')).length,
    1,
  )
  assert.equal(
    server.log.filter((request) => request.url.includes('/api/storage/apps/')).length,
    0,
  )
})

test('content listings page at the server byte-budget boundary', async () => {
  const { server } = freshEnv()
  globalThis.indexedDB = {
    open() { throw new DOMException('denied', 'SecurityError') },
  }
  const s = await newStorage()
  for (let i = 0; i < 33; i++) {
    server.seed(`records/${String(i).padStart(2, '0')}.json`, { id: i })
  }

  const entries = await s.list('records', { includeContent: true })
  assert.equal(entries.length, 33)
  assert.equal(entries.every(entry => Object.hasOwn(entry, 'content')), true)
  const listRequests = server.log.filter(
    request => request.url.includes('/apps-list/'),
  )
  assert.equal(listRequests.length, 3)
  assert.equal(listRequests.every(request => request.url.includes('limit=16')), true)
})

test('batched list content keeps a newer queued JSON write visible', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('records/a.json', { value: 'server' })
  server.forceWrite('records/a.json', 503)
  assert.deepEqual(
    await s.set('records/a.json', { value: 'queued' }),
    { queued: true },
  )

  const entries = await s.list('records', { includeContent: true })
  assert.deepEqual(entries[0].content, { value: 'queued' })
  assert.deepEqual(server.serverValue('records/a.json'), { value: 'server' })
})

test('opaque-frame direct storage preserves text, blob, delete, CAS, and subscriptions', async () => {
  const { server } = freshEnv()
  globalThis.indexedDB = { open() { throw new DOMException('denied', 'SecurityError') } }
  const s = await newStorage()

  const observed = recordSubscription((cb) => s.subscribeText('note.txt', cb))
  await s.setText('note.txt', 'hello', { contentType: 'text/markdown' })
  await waitFor(() => observed.values.includes('hello'))
  assert.equal(await s.getText('note.txt'), 'hello')

  const blob = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' })
  assert.deepEqual(await s.setBlob('image.bin', blob), { synced: true })
  const loaded = await s.getBlob('image.bin')
  assert.equal(loaded.type, 'image/png')
  assert.deepEqual([...new Uint8Array(await loaded.arrayBuffer())], [1, 2, 3])

  const first = await s.durableWrite('cas.json', { n: 1 }, { ifNoneMatch: true })
  assert.equal(first.durability, 'synced')
  assert.ok(first.version)
  const versioned = await s.getWithVersion('cas.json')
  assert.deepEqual(versioned.value, { n: 1 })
  const second = await s.durableWrite('cas.json', { n: 2 }, { ifMatch: versioned.version })
  assert.equal(second.durability, 'synced')
  assert.ok(second.version)
  await assert.rejects(
    s.durableWrite('cas.json', { n: 3 }, { ifMatch: versioned.version }),
    (error) => error.status === 412,
  )

  assert.deepEqual(await s.remove('note.txt'), { synced: true })
  await waitFor(() => observed.values.at(-1) === null)
  assert.equal(server.serverHas('note.txt'), false)
  observed.unsub()
})

async function renderUseDocument(storage, path, opts) {
  const stateSlots = []
  const refSlots = []
  const effects = []
  let stateIndex = 0
  let refIndex = 0
  const React = {
    useState(init) {
      const i = stateIndex++
      if (!(i in stateSlots)) stateSlots[i] = typeof init === 'function' ? init() : init
      const setState = (next) => {
        stateSlots[i] = typeof next === 'function' ? next(stateSlots[i]) : next
      }
      return [stateSlots[i], setState]
    },
    useRef(init) {
      const i = refIndex++
      if (!(i in refSlots)) refSlots[i] = { current: init }
      return refSlots[i]
    },
    useCallback(fn) { return fn },
    useEffect(fn) { effects.push(fn) },
  }
  const { createUseDocument } = await runtimeExports()
  const useDocument = createUseDocument(storage, React)
  const handle = useDocument(path, opts)
  const cleanups = effects.map((fn) => fn()).filter(Boolean)
  return { handle, state: () => stateSlots[0], cleanup: () => cleanups.forEach((fn) => fn()) }
}

// ── Read-through cache: write/read online, read offline ────────────────────

test('a value written online is readable offline (write-through mirror)', async () => {
  const { server } = freshEnv()
  const s = await newStorage()

  const r = await s.set('note.json', { text: 'hello' })
  assert.deepEqual(r, { synced: true })
  assert.equal(server.serverHas('note.json'), true)

  server.setOnline(false)
  assert.deepEqual(await s.get('note.json'), { text: 'hello' })
})

test('an online get() mirrors the server value so a later offline get() serves it', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('seeded.json', { from: 'server' })

  // First read is online → mirrors into the cache store.
  assert.deepEqual(await s.get('seeded.json'), { from: 'server' })

  server.setOnline(false)
  assert.deepEqual(await s.get('seeded.json'), { from: 'server' })
})

test('a never-fetched path reads null offline (no value claimed it never had)', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)
  assert.equal(await s.get('unknown.json'), null)
})

test('an offline delete tombstones the mirror so a later offline read returns null', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  await s.set('gone.json', { v: 1 })       // online write → mirror present

  server.setOnline(false)
  const r = await s.remove('gone.json')
  assert.deepEqual(r, { queued: true })    // delete queued, not yet synced
  // Read-your-writes: the offline read sees the tombstone, not the old value.
  assert.equal(await s.get('gone.json'), null)
})

// ── Outbox: queue while offline, drain on reconnect, in order ──────────────

test('an offline write queues, then drains to the server on reconnect', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  await s.set('k.json', { v: 'first' })    // online baseline on the server

  server.setOnline(false)
  const r = await s.set('k.json', { v: 'edited offline' })
  assert.deepEqual(r, { queued: true })
  assert.equal(await s.pendingCount(), 1)
  assert.deepEqual(server.serverValue('k.json'), { v: 'first' })  // server untouched

  server.setOnline(true)
  await s._drain()
  assert.equal(await s.pendingCount(), 0)
  assert.deepEqual(server.serverValue('k.json'), { v: 'edited offline' })
})

test('the outbox coalesces to one op per path; the last write wins on drain', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)

  await s.set('y.json', { n: 'a' })
  await s.set('y.json', { n: 'b' })
  await s.set('y.json', { n: 'c' })
  assert.equal(await s.pendingCount(), 1)   // coalesced, not 3

  server.setOnline(true)
  await s._drain()
  assert.deepEqual(server.serverValue('y.json'), { n: 'c' })  // LWW
})

test('offline signal batches from separate runtimes do not coalesce', async () => {
  const { server } = freshEnv()
  const first = await newStorage('7')
  const second = await newStorage('7')
  server.setOnline(false)

  await first._queueSignals([{ id: 'first', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])
  await second._queueSignals([{ id: 'second', occurred_at: '2026-07-13T10:00:01Z', name: 'item_created', payload: {} }])
  assert.equal(await first.pendingCount(), 0) // telemetry never changes user-data sync state
  assert.equal(await first._pendingSignalCount(), 2)

  server.setOnline(true)
  await first._drainSignals()
  assert.equal(await first._pendingSignalCount(), 0)
  assert.deepEqual(server.signalEvents.map((event) => event.id), ['first', 'second'])
})

test('signal queues are isolated by immutable app installation identity', async () => {
  const { server } = freshEnv()
  const oldInstall = await newStorage('7', { appInstanceId: 'install-old' })
  const newInstall = await newStorage('7', { appInstanceId: 'install-new' })
  server.setOnline(false)

  await oldInstall._queueSignals([{ id: 'old', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])
  await newInstall._queueSignals([{ id: 'new', occurred_at: '2026-07-13T10:00:01Z', name: 'app_ready', payload: {} }])
  assert.equal(await oldInstall._pendingSignalCount(), 1)
  assert.equal(await newInstall._pendingSignalCount(), 1)

  server.setOnline(true)
  await newInstall._drainSignals()
  assert.deepEqual(server.signalEvents.map((event) => event.id), ['new'])
  assert.equal(await oldInstall._pendingSignalCount(), 1)
})

test('storage queues and caches are isolated by exact installation identity', async () => {
  const { server } = freshEnv()
  const legacy = await newStorage('7')
  const current = await newStorage('7', { appInstanceId: 'install-current' })
  server.setOnline(false)

  await legacy.set('legacy.json', { owner: 'legacy' })
  await current.set('current.json', { owner: 'current' })

  assert.equal(await legacy.pendingCount(), 1)
  assert.equal(await current.pendingCount(), 1)
  assert.equal(await legacy.get('current.json'), null)
  assert.equal(await current.get('legacy.json'), null)
})

test('an old runtime cannot refresh across a rotated storage generation', async () => {
  const { server } = freshEnv()
  let nonce = 'generation-old'
  const storage = await newStorage('7', {
    appInstanceId: 'generation-old',
    getToken: async () => appToken('7', nonce),
  })
  server.setOnline(false)
  await storage.set('wiped.json', { mustNotReturn: true })

  // DELETE /apps/{id}/data rotates the nonce before releasing the server's
  // storage lock. Even if the host can now mint the new token, this old runtime
  // remains bound to its init identity and cannot replay the pre-wipe write.
  nonce = 'generation-new'
  server.setOnline(true)
  await storage._drain()
  assert.equal(server.serverHas('wiped.json'), false)
  assert.equal(await storage.pendingCount(), 1)
})

test('nonce-aware signal runtime ignores ambiguous legacy records after ID reuse', async () => {
  const { server } = freshEnv()
  const legacy = await newStorage('7')
  server.setOnline(false)
  await legacy._queueSignals([{ id: 'legacy', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])

  const replacement = await newStorage('7', { appInstanceId: 'replacement-install' })
  assert.equal(await replacement._pendingSignalCount(), 0)
  server.setOnline(true)
  await replacement._drainSignals()
  assert.deepEqual(server.signalEvents, [])
})

test('a 401 refreshes the app token and retries a storage write once', async () => {
  const { server } = freshEnv()
  const calls = []
  const storage = await newStorage('7', {
    appInstanceId: 'install-one',
    getToken: async (options = {}) => {
      calls.push(options)
      return appToken('7', 'install-one', options.forceRefresh ? 'fresh' : 'stale')
    },
  })
  server.forceWrite('refresh.json', 401)

  assert.deepEqual(await storage.set('refresh.json', { saved: true }), { synced: true })
  assert.ok(calls.some((options) => options.forceRefresh === true))
  assert.deepEqual(server.serverValue('refresh.json'), { saved: true })
})

test('signal rollout 404 is retryable and preserves the durable queue', async () => {
  const { server } = freshEnv()
  const storage = await newStorage('7', { appInstanceId: 'install-one' })
  server.setSignalStatus(404)
  await storage._queueSignals([{ id: 'rollout', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])
  await storage._drainSignals()
  assert.equal(await storage._pendingSignalCount(), 1)
})

test('a forbidden signal batch is poison and does not retry forever', async () => {
  const { server } = freshEnv()
  const storage = await newStorage('7', { appInstanceId: 'install-one' })
  server.setSignalStatus(403)
  await storage._queueSignals([{ id: 'forbidden', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])

  await storage._drainSignals()

  assert.equal(await storage._pendingSignalCount(), 0)
  assert.deepEqual(server.signalEvents, [])
})

test('init reuses one runtime per installation and updates its token broker', async () => {
  const { server } = freshEnv()
  const previousWindow = globalThis.window
  const previousDocument = globalThis.document
  const windowListeners = new Map()
  const documentListeners = new Map()
  const track = (registry, type, cb) => {
    if (!registry.has(type)) registry.set(type, new Set())
    registry.get(type).add(cb)
  }
  const untrack = (registry, type, cb) => registry.get(type)?.delete(cb)
  globalThis.window = {
    location: { origin: 'https://mobius.test' },
    parent: { postMessage() {} },
    addEventListener(type, cb) { track(windowListeners, type, cb) },
    removeEventListener(type, cb) { untrack(windowListeners, type, cb) },
  }
  globalThis.document = {
    visibilityState: 'visible',
    addEventListener(type, cb) { track(documentListeners, type, cb) },
    removeEventListener(type, cb) { untrack(documentListeners, type, cb) },
  }
  try {
    const runtime = await import(`../../../public/mobius-runtime.js?init-once=${Date.now()}`)
    const firstToken = appToken('7', 'install-one', 'first')
    const secondToken = appToken('7', 'install-one', 'second')
    const first = runtime.init({
      appId: '7',
      appInstanceId: 'install-one',
      getToken: async () => firstToken,
    })
    const listenerCounts = () => ({
      window: [...windowListeners].map(([type, callbacks]) => [type, callbacks.size]),
      document: [...documentListeners].map(([type, callbacks]) => [type, callbacks.size]),
    })
    const before = listenerCounts()

    const second = runtime.init({
      appId: '7',
      appInstanceId: 'install-one',
      getToken: async () => secondToken,
    })

    assert.equal(second, first)
    assert.deepEqual(listenerCounts(), before)
    await second.storage.set('uses-latest-token.json', { ok: true })
    const write = server.log.find((entry) => entry.method === 'PUT' && entry.url.includes('uses-latest-token.json'))
    assert.equal(write.headers.Authorization, `Bearer ${secondToken}`)
    second.signal._destroy()
    second.storage._destroy()
  } finally {
    globalThis.window = previousWindow
    globalThis.document = previousDocument
  }
})

test('explicit data wipe purges one app runtime state without touching another', async () => {
  const { server } = freshEnv()
  const removed = await newStorage('7', { appInstanceId: 'removed-install' })
  const retained = await newStorage('8', { appInstanceId: 'retained-install' })
  server.setOnline(false)
  await removed.set('note.json', { text: 'remove me' })
  await removed._queueSignals([{ id: 'removed', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])
  await retained.set('note.json', { text: 'keep me' })
  await retained._queueSignals([{ id: 'retained', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])

  const { purgeAppRuntimeData } = await runtimeExports()
  await purgeAppRuntimeData('7')
  assert.equal(await removed.pendingCount(), 0)
  assert.equal(await removed._pendingSignalCount(), 0)
  assert.equal(await retained.pendingCount(), 1)
  assert.equal(await retained._pendingSignalCount(), 1)
})

test('a failing signal endpoint cannot block or dead-letter user storage writes', async () => {
  const { server } = freshEnv()
  const storage = await newStorage('7')
  server.setSignalStatus(503)

  await storage._queueSignals([{ id: 'stuck', occurred_at: '2026-07-13T10:00:00Z', name: 'app_ready', payload: {} }])
  await storage._drainSignals()
  assert.equal(await storage._pendingSignalCount(), 1)

  const result = await storage.set('document.json', { saved: true })
  assert.deepEqual(result, { synced: true })
  assert.deepEqual(server.serverValue('document.json'), { saved: true })
  assert.equal(await storage.pendingCount(), 0)
  assert.equal(await storage._pendingSignalCount(), 1)
})

test('the offline signal queue evicts oldest events at its per-app cap', async () => {
  const { server } = freshEnv()
  const storage = await newStorage('7')
  server.setOnline(false)
  const events = Array.from({ length: 600 }, (_, i) => ({
    id: `signal-${i}`,
    occurred_at: '2026-07-13T10:00:00Z',
    name: 'item_created',
    payload: {},
  }))

  await storage._queueSignals(events)
  assert.equal(await storage._pendingSignalCount(), 500)
})

test('the offline signal queue is bounded by serialized bytes as well as count', async () => {
  const { server } = freshEnv()
  const storage = await newStorage('7')
  server.setOnline(false)
  const events = Array.from({ length: 300 }, (_, i) => ({
    id: `wide-${i}`,
    occurred_at: '2026-07-13T10:00:00Z',
    name: 'error',
    payload: { message: 'x'.repeat(10000) },
  }))

  await storage._queueSignals(events)
  assert.ok(await storage._pendingSignalCount() < 300)
  assert.ok(await storage._pendingSignalCount() > 0)
})

test('the shared signal database has an owner-wide cap across many apps', async () => {
  const { server } = freshEnv()
  server.setOnline(false)
  const storages = await Promise.all(Array.from({ length: 5 }, (_, index) => (
    newStorage(String(index + 1), { appInstanceId: `install-${index + 1}` })
  )))
  for (let appIndex = 0; appIndex < storages.length; appIndex += 1) {
    await storages[appIndex]._queueSignals(Array.from({ length: 500 }, (_, eventIndex) => ({
      id: `${appIndex}-${eventIndex}`,
      occurred_at: '2026-07-13T10:00:00Z',
      name: 'item_created',
      payload: {},
    })))
  }
  const counts = await Promise.all(storages.map((storage) => storage._pendingSignalCount()))
  assert.equal(counts.reduce((sum, count) => sum + count, 0), 2000)
  assert.ok(counts.every((count) => count <= 500))
})

test('a transient failure on the head op stops the drain and preserves order', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)
  await s.set('a.json', { n: 1 })
  await s.set('b.json', { n: 2 })
  assert.equal(await s.pendingCount(), 2)

  server.setOnline(true)
  server.forceWrite('a.json', 503)          // head op fails transiently
  // _drain() returns its drain promise, so awaiting it fences on the pass
  // FINISHING: drainInner attempts the head op, hits the 503, and breaks
  // (order-preserving). pendingCount stays 2 here, so the drain promise — not a
  // count — is the settle signal.
  await s._drain()
  // Order preserved: b.json must NOT have been sent ahead of the stuck a.json.
  assert.equal(server.log.some((e) => e.method === 'PUT' && e.url.includes('b.json')), false)
  assert.equal(await s.pendingCount(), 2)    // both still queued
  assert.equal(server.serverHas('a.json'), false)
  assert.equal(server.serverHas('b.json'), false)

  // Next drain (no forced failure) flushes both, a before b.
  await s._drain()
  assert.equal(await s.pendingCount(), 0)
  assert.deepEqual(server.serverValue('a.json'), { n: 1 })
  assert.deepEqual(server.serverValue('b.json'), { n: 2 })
})

test('a DELETE that 404s counts as synced (already-absent is the intended end state)', async () => {
  freshEnv()
  const s = await newStorage()
  const r = await s.remove('never-existed.json')
  assert.deepEqual(r, { synced: true })
  assert.equal(await s.pendingCount(), 0)
})

// ── subscribe(): initial value, then on change, with the delivered tie-break ─

test('subscribe() fires the initial value, then again on every set()', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('a.json', { v: 0 })

  const seen = []
  const unsub = s.subscribe('a.json', (v) => seen.push(v))
  await waitFor(() => seen.length >= 1)
  assert.deepEqual(seen, [{ v: 0 }])        // initial

  await s.set('a.json', { v: 1 })
  await waitFor(() => seen.length >= 2)
  assert.deepEqual(seen, [{ v: 0 }, { v: 1 }])

  await s.set('a.json', { v: 2 })
  await waitFor(() => seen.length >= 3)
  assert.deepEqual(seen, [{ v: 0 }, { v: 1 }, { v: 2 }])
  unsub()

  // After unsubscribe, no further fires. notify() runs SYNCHRONOUSLY inside the
  // write (writeLocal notifies before set() resolves), so the awaited set() is
  // itself the fence — a fire to a still-subscribed cb would already have landed
  // by the time it resolves. A buggy unsub that left the cb attached surfaces
  // here with no fixed delay.
  await s.set('a.json', { v: 3 })
  assert.deepEqual(seen, [{ v: 0 }, { v: 1 }, { v: 2 }])
})

test('subscribe() initial fires null on an absent path, then on remove() fires null', async () => {
  freshEnv()
  const s = await newStorage()
  await s.set('r.json', { v: 1 })

  const seen = []
  const unsub = s.subscribe('r.json', (v) => seen.push(v))
  await waitFor(() => seen.length >= 1)
  assert.deepEqual(seen, [{ v: 1 }])

  await s.remove('r.json')
  await waitFor(() => seen.length >= 2)
  assert.deepEqual(seen, [{ v: 1 }, null])  // remove notifies null
  unsub()
})

test('a set() racing a slow initial get() wins the tie-break (notify-last, no stale)', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  // The server holds an older value the initial get() will (slowly) resolve.
  server.seed('t.json', { stale: true })

  const seen = []
  const unsub = s.subscribe('t.json', (v) => seen.push(v))
  // Fire the set BEFORE awaiting the initial get — it must win.
  await s.set('t.json', { fresh: true })
  // Fence on a path-serialized op, not a fixed delay: get() runs under the same
  // per-path lock as the subscribe's initial get(), so once this resolves that
  // initial get() has fully resolved (delivered or self-suppressed) — exactly
  // the settle the tie-break assertion needs.
  await s.get('t.json')

  // The subscriber must END on the fresh value — the stale initial must never
  // land after the set's notify (the `delivered` flag's whole job).
  assert.deepEqual(seen[seen.length - 1], { fresh: true })
  const staleAfterFresh =
    seen.findIndex((v) => v && v.stale) > seen.findIndex((v) => v && v.fresh) &&
    seen.findIndex((v) => v && v.fresh) !== -1
  assert.equal(staleAfterFresh, false)
  unsub()
})

test('one throwing subscriber does not break delivery to the others', async () => {
  freshEnv()
  const s = await newStorage()
  const good = []
  const unsubBad = s.subscribe('s.json', () => { throw new Error('listener boom') })
  const unsubGood = s.subscribe('s.json', (v) => good.push(v))
  await waitFor(() => good.length >= 1)
  await s.set('s.json', { ok: true })
  await waitFor(() => good.length >= 2)
  // The good subscriber still got both its initial (null) and the set value.
  assert.deepEqual(good, [null, { ok: true }])
  unsubBad()
  unsubGood()
})

// ── drainInner poison-op: dead-letter + reconcile the mirror ───────────────

test('a 4xx-poison write is dropped AND the mirror reconciles to the server value', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('p.json', { server: 'truth' })
  await s.get('p.json')                     // prime the mirror online

  const seen = []
  const unsub = s.subscribe('p.json', (v) => seen.push(v))
  await waitFor(() => seen.length >= 1)
  assert.deepEqual(seen, [{ server: 'truth' }])

  server.forceWrite('p.json', 422)          // the next PUT is poison (fatal 4xx)
  const r = await s.set('p.json', { client: 'bad' })
  // settle() reports {synced} because the op was dead-lettered (removed) — the
  // dead-letter reconcile is what actually re-syncs the mirror, below.
  assert.deepEqual(r, { synced: true })
  // Wait on the reconcile END-STATE: the dead-letter must drop the pending op
  // AND the subscriber must observe the optimistic value then the reconcile
  // back to the server value (3 notifies total). Polling both conditions is
  // deterministic where a fixed tick(20) raced the scheduled reconcile.
  await waitFor(async () =>
    (await s.pendingCount()) === 0 &&
    seen.length >= 3 &&
    JSON.stringify(seen[seen.length - 1]) === JSON.stringify({ server: 'truth' })
  )

  // The poison op is dropped (not stuck retrying forever).
  assert.equal(await s.pendingCount(), 0)
  // The server NEVER accepted the bad write.
  assert.deepEqual(server.serverValue('p.json'), { server: 'truth' })
  // The optimistic mirror is reconciled back to the authoritative server value,
  // and the subscriber observed the optimistic value then the reconcile.
  assert.deepEqual(seen[0], { server: 'truth' })   // initial
  assert.deepEqual(seen[1], { client: 'bad' })     // optimistic (read-your-writes)
  assert.deepEqual(seen[seen.length - 1], { server: 'truth' })  // reconciled
  unsub()

  // And an offline read now serves the reconciled (server) value, not the
  // refused optimistic one.
  server.setOnline(false)
  assert.deepEqual(await s.get('p.json'), { server: 'truth' })
})

test('a poison write does not head-of-line-block a later good write for another path', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)
  await s.set('poison.json', { bad: true })
  await s.set('good.json', { ok: true })
  assert.equal(await s.pendingCount(), 2)

  server.setOnline(true)
  server.forceWrite('poison.json', 422)     // first op is poison
  await s._drain()

  // The poison op is dead-lettered; the good op still drains (kept draining
  // past the dropped op rather than wedging the queue).
  assert.equal(await s.pendingCount(), 0)
  assert.equal(server.serverHas('poison.json'), false)
  assert.deepEqual(server.serverValue('good.json'), { ok: true })
})

// ── withPathLock: per-path serialization (no interleave) ───────────────────

test('two concurrent set()s on one path serialize; the mirror ends at the last call', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  // Fire both WITHOUT awaiting between them — withPathLock must order them.
  const p1 = s.set('x.json', { n: 1 })
  const p2 = s.set('x.json', { n: 2 })
  await Promise.all([p1, p2])
  await waitFor(async () => (await s.pendingCount()) === 0)

  server.setOnline(false)
  assert.deepEqual(await s.get('x.json'), { n: 2 })   // last call wins the mirror
  server.setOnline(true)
  assert.deepEqual(server.serverValue('x.json'), { n: 2 })
})

test('a get() interleaved with two writes never strands the mirror on a mid value', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('m.json', { n: 0 })
  // Issue read-write-read-write all unawaited; the per-path chain orders them so
  // the final mirror is the last write, not a read's late cache-write.
  const ops = [
    s.get('m.json'),
    s.set('m.json', { n: 1 }),
    s.get('m.json'),
    s.set('m.json', { n: 2 }),
  ]
  await Promise.all(ops)
  await waitFor(async () => (await s.pendingCount()) === 0)
  server.setOnline(false)
  assert.deepEqual(await s.get('m.json'), { n: 2 })
})

// ── Typed reads: a wrong-kind read fails loud, not silently corrupt ────────

test('reading a json path with getText() throws a clear typed error', async () => {
  freshEnv()
  const s = await newStorage()
  await s.set('j.json', { a: 1 })
  await assert.rejects(() => s.getText('j.json'), /holds json|read it with get\(\)/)
})

// ── Logout: deleting the shared DB purges the read-through cache mirror ─────

test('deleting the mobius-outbox DB (logout) purges the cache mirror too', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  await s.set('cached.json', { v: 1 })      // populates BOTH outbox-drained + cache store
  server.setOnline(false)
  assert.deepEqual(await s.get('cached.json'), { v: 1 })  // mirror present
  server.setOnline(true)

  // Mirror client.js delOutboxDb(): deleteDatabase on the shared DB.
  await new Promise((resolve) => {
    const req = indexedDB.deleteDatabase('mobius-outbox')
    req.onsuccess = req.onerror = req.onblocked = () => resolve()
  })

  // A fresh runtime (post-logout) reads offline → the mirror is gone (null),
  // proving the cache store rode the same DB and was purged, not just the outbox.
  globalThis.indexedDB = globalThis.indexedDB   // same factory; DB was deleted
  const s2 = await newStorage()
  server.setOnline(false)
  assert.equal(await s2.get('cached.json'), null)
})


test('legacy set() keeps an exact {synced}/{queued} shape over a dead-lettered write', async () => {
  const { server } = freshEnv()
  const s = await newStorage()

  server.forceWrite('legacy.json', 413)
  const r = await s.set('legacy.json', { too: 'large' })
  assert.deepEqual(Object.keys(r), ['synced'])
  assert.deepEqual(r, { synced: true })
  assert.equal(await s.pendingCount(), 0)
})

test('durableWrite resolves synced only after an accepted server write', async () => {
  const { server } = freshEnv()
  const s = await newStorage()

  const r = await s.durableWrite('durable.json', { ok: true })
  assert.equal(r.durability, 'synced')
  assert.equal(r.path, 'durable.json')
  assert.equal(typeof r.writeId, 'string')
  assert.deepEqual(server.serverValue('durable.json'), { ok: true })
})

test('durableWrite resolves queued when the write is durably outboxed offline', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)

  const r = await s.durableWrite('queued.json', { offline: true })
  assert.equal(r.durability, 'queued')
  assert.equal(await s.pendingCount(), 1)
  assert.equal(server.serverHas('queued.json'), false)
})

test('durableWrite rejects with DurableWriteError on fatal dead-letter status', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  const { DurableWriteError } = await runtimeExports()

  server.forceWrite('refused.json', 413)
  await assert.rejects(
    () => s.durableWrite('refused.json', { bad: true }),
    (err) => err instanceof DurableWriteError &&
      err.code === 'dead_letter' &&
      err.status === 413 &&
      err.path === 'refused.json' &&
      err.retryable === false,
  )
  assert.equal(await s.pendingCount(), 0)
})

test('412 CAS conflict is retryable and does not emit dead-letter', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  const { DurableWriteError } = await runtimeExports()
  const deadLetters = []
  const unsub = s.onDeadLetter((dl) => deadLetters.push(dl))

  server.forceWrite('conflict.json', 412)
  await assert.rejects(
    () => s.durableWrite('conflict.json', { stale: true }, { ifMatch: '"old"' }),
    (err) => err instanceof DurableWriteError &&
      err.code === 'conflict' &&
      err.status === 412 &&
      err.retryable === true,
  )
  await waitFor(async () => (await s.pendingCount()) === 0)
  assert.equal(await s.pendingCount(), 0)
  assert.deepEqual(deadLetters, [])
  unsub()
})

// ── Compare-and-swap: getWithVersion + conditional durableWrite ────────────

test('getWithVersion returns the value AND its server version (ETag)', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('index.json', [{ id: 'a' }])

  const { value, version } = await s.getWithVersion('index.json')
  assert.deepEqual(value, [{ id: 'a' }])
  assert.equal(typeof version, 'string')
  assert.ok(version.length > 0)               // the ETag the server handed back

  // The versioned read opted in with X-Mobius-Version:1 (that's what makes the
  // server echo the ETag) — a plain get() must NOT, so it stays a cheap read.
  const vget = server.log.filter((e) => e.method === 'GET' && e.url.includes('index.json')).pop()
  assert.equal(vget.headers['X-Mobius-Version'], '1')
})

test('durableWrite({ifMatch}) sends If-Match and succeeds when the version matches', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('doc.json', { n: 0 })

  const { version } = await s.getWithVersion('doc.json')
  const r = await s.durableWrite('doc.json', { n: 1 }, { ifMatch: version })

  assert.equal(r.durability, 'synced')
  assert.deepEqual(server.serverValue('doc.json'), { n: 1 })
  // The conditional write carried the held version as an If-Match precondition.
  const put = server.log.filter((e) => e.method === 'PUT' && e.url.includes('doc.json')).pop()
  assert.equal(put.headers['If-Match'], version)
  // ...and the accepted write returns the NEW version for the next CAS round.
  assert.equal(typeof r.version, 'string')
  assert.notEqual(r.version, version)
})

test('a stale ifMatch surfaces as a retryable conflict; a re-read + retry lands both edits', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  const { DurableWriteError } = await runtimeExports()
  server.seed('topics.json', [{ t: 'a' }])

  // We read at version V1, then a CONCURRENT writer (cron/agent) moves the ETag.
  const first = await s.getWithVersion('topics.json')
  server.seed('topics.json', [{ t: 'a' }, { t: 'cron' }])   // now at V2 on the server

  // Our conditional write on the stale V1 is rejected as a retryable conflict,
  // NOT silently last-write-wins (which would drop the cron edit).
  await assert.rejects(
    () => s.durableWrite('topics.json', [{ t: 'a' }, { t: 'ui' }], { ifMatch: first.version }),
    (err) => err instanceof DurableWriteError &&
      err.code === 'conflict' &&
      err.status === 412 &&
      err.retryable === true,
  )
  assert.equal(await s.pendingCount(), 0)                    // the conflicted op is not stuck
  // The server still holds the concurrent writer's value — our stale write never landed.
  assert.deepEqual(server.serverValue('topics.json'), [{ t: 'a' }, { t: 'cron' }])

  // The app owns the retry: re-read at the fresh version, merge, write again.
  const second = await s.getWithVersion('topics.json')
  assert.notEqual(second.version, first.version)
  const merged = [...second.value, { t: 'ui' }]
  const r = await s.durableWrite('topics.json', merged, { ifMatch: second.version })

  assert.equal(r.durability, 'synced')
  // Both edits survive — the whole point of CAS over last-write-wins.
  assert.deepEqual(server.serverValue('topics.json'), [{ t: 'a' }, { t: 'cron' }, { t: 'ui' }])
})

test('ifNoneMatch:true is a create-only write that conflicts when the path already exists', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  const { DurableWriteError } = await runtimeExports()

  const created = await s.durableWrite('new.json', { first: true }, { ifNoneMatch: true })
  assert.equal(created.durability, 'synced')
  assert.deepEqual(server.serverValue('new.json'), { first: true })

  await assert.rejects(
    () => s.durableWrite('new.json', { clobber: true }, { ifNoneMatch: true }),
    (err) => err instanceof DurableWriteError && err.code === 'conflict' && err.status === 412,
  )
  assert.deepEqual(server.serverValue('new.json'), { first: true })   // not clobbered
})

test('plain set()/durableWrite (last-write-wins) send NO If-Match — CAS is opt-in', async () => {
  const { server } = freshEnv()
  const s = await newStorage()

  await s.set('plain.json', { a: 1 })
  await s.durableWrite('plain2.json', { b: 2 })

  assert.deepEqual(server.serverValue('plain.json'), { a: 1 })
  assert.deepEqual(server.serverValue('plain2.json'), { b: 2 })
  for (const name of ['plain.json', 'plain2.json']) {
    const put = server.log.filter((e) => e.method === 'PUT' && e.url.includes(name)).pop()
    assert.equal(put.headers['If-Match'], undefined)
    assert.equal(put.headers['If-None-Match'], undefined)
  }
})

test('onDeadLetter replays an offline-queued write later refused on drain', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.setOnline(false)
  const queued = await s.durableWrite('later-refused.json', { bad: true })
  assert.equal(queued.durability, 'queued')

  server.forceWrite('later-refused.json', 413)
  server.setOnline(true)
  s._drain()
  await waitFor(async () => (await s.pendingCount()) === 0)

  const seen = []
  const unsub = s.onDeadLetter((dl) => seen.push(dl))
  await waitFor(() => seen.length === 1)
  assert.equal(seen[0].path, 'later-refused.json')
  assert.equal(seen[0].status, 413)
  assert.deepEqual(seen[0].refusedValue, { bad: true })
  unsub()
})

test('useDocument factory requires an explicit React (window.mobius.createUseDocument binding) — no window.React fallback', async () => {
  freshEnv()
  const s = await newStorage()
  const { createUseDocument } = await runtimeExports()
  // window.mobius exposes `createUseDocument: (React) => createUseDocument(storage, React)`,
  // so apps bind their OWN imported React. Called without one (and no host sets a
  // window.React global), the returned hook must throw a guiding error rather than
  // silently lean on an absent global — the bug the exposure fix closed.
  const useDocNoReact = createUseDocument(s)
  assert.throws(() => useDocNoReact('x.json', {}), /createUseDocument\(React\)/)
})

test('useDocument serializes updates and reconciles item ids by content identity', async () => {
  const { server } = freshEnv()
  const s = await newStorage()
  server.seed('doc.json', [{ text: 'server' }])

  const doc = await renderUseDocument(s, 'doc.json', {
    initial: [{ id: 'local-stable', text: 'server' }],
    identity: (item) => item.text,
    mode: 'lww',
  })
  await waitFor(() => doc.state().status === 'ready')
  assert.deepEqual(doc.state().value, [{ id: 'local-stable', text: 'server' }])

  const order = []
  const p1 = doc.handle.update((items) => {
    order.push('first')
    return [...items, { id: 'a', text: 'a' }]
  })
  const p2 = doc.handle.update((items) => {
    order.push('second')
    return [...items, { id: 'b', text: 'b' }]
  })
  await Promise.all([p1, p2])

  assert.deepEqual(order, ['first', 'second'])
  assert.deepEqual(server.serverValue('doc.json').map((item) => item.text), ['server', 'a', 'b'])
  assert.equal(server.serverValue('doc.json')[0].id, 'local-stable')
  doc.cleanup()
})
