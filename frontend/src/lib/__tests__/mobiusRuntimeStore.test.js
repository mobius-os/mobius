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
import { freshEnv, tick, waitFor } from './mobiusRuntimeHarness.mjs'

// makeStorage is imported lazily inside each test AFTER freshEnv() installs the
// browser globals it reads at construction time. A top-level import would run
// before the globals exist.
async function newStorage(appId = '1') {
  const { makeStorage } = await import('../../../public/mobius-runtime.js')
  return makeStorage({ appId, getToken: async () => 'test-token' })
}

async function runtimeExports() {
  return import('../../../public/mobius-runtime.js')
}

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
  s._drain()
  await tick(20)
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
  s._drain()
  await tick(20)
  assert.deepEqual(server.serverValue('y.json'), { n: 'c' })  // LWW
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
  s._drain()
  await tick(20)
  // Both still queued — b.json must NOT have been sent ahead of a.json.
  assert.equal(await s.pendingCount(), 2)
  assert.equal(server.serverHas('a.json'), false)
  assert.equal(server.serverHas('b.json'), false)

  // Next drain (no forced failure) flushes both, a before b.
  s._drain()
  await tick(20)
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

  // After unsubscribe, no further fires. There's no positive end-state to wait
  // on (we're asserting an ABSENCE), so a fixed settle is the right tool here —
  // give any erroneous fire a window to land, then assert it didn't.
  await s.set('a.json', { v: 3 })
  await tick(5)
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
  await tick(30)   // let the initial get() + any revalidate settle

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
  s._drain()
  await tick(20)

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
  await tick(10)

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
  await tick(20)
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
  await tick(10)
  assert.equal(await s.pendingCount(), 0)
  assert.deepEqual(deadLetters, [])
  unsub()
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
