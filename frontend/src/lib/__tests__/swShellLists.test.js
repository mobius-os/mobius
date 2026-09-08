// Exercise the installed Workbox strategies through their public request lifecycle.
import { test } from 'node:test'
import assert from 'node:assert/strict'

globalThis.self = { location: { href: 'https://mobius.test/sw.js', origin: 'https://mobius.test' } }
globalThis.location = self.location
globalThis.caches = {}
Object.defineProperty(self, "caches", { get: () => globalThis.caches })
globalThis.FetchEvent = class FetchEvent {}
globalThis.ExtendableEvent = class ExtendableEvent { waitUntil() {} }
const { LiveShellList } = await import('../../sw-shell-lists.js')
const { NetworkFirst } = await import('workbox-strategies/NetworkFirst.js')
const { requiresLiveShellList, SHELL_DATA_CACHE } = await import('../../sw-cache-policy.js')

function fixture(t) {
  const rows = new Map()
  const key = request => typeof request === 'string' ? request : request.url
  const cache = {
    match: async request => rows.get(key(request))?.clone(),
    put: async (request, response) => { rows.set(key(request), response.clone()) },
    keys: async () => [...rows.keys()].map(url => new Request(url)),
    delete: async request => rows.delete(key(request)),
  }
  t.mock.method(globalThis, 'fetch', async () => new Response('[{"id":"fresh"}]'))
  t.mock.property(globalThis, 'caches', { open: async () => cache, match: request => cache.match(request) })
  const live = new LiveShellList({ cacheName: SHELL_DATA_CACHE })
  const ordinary = new NetworkFirst({ cacheName: SHELL_DATA_CACHE })
  async function read(request) {
    const strategy = requiresLiveShellList(request) ? live : ordinary
    const [response, done] = strategy.handleAll({ request, event: new ExtendableEvent() })
    const result = await Promise.allSettled([response, done])
    if (result[0].status === 'rejected') throw result[0].reason
    assert.equal(result[1].status, 'fulfilled')
    return result[0].value
  }
  return { cache, rows, read }
}

for (const path of ['/api/chats', '/api/apps/']) {
  test(`${path}: live success replaces the offline snapshot, including after invalidation`, async t => {
    const f = fixture(t)
    const ordinary = new Request(`https://mobius.test${path}`)
    const live = new Request(ordinary, { cache: 'no-store' })
    for (const invalidate of [false, true]) {
      await f.cache.put(ordinary, new Response('[{"id":"stale"}]'))
      if (invalidate) await f.cache.delete(ordinary)
      t.mock.method(globalThis, 'fetch', async () => new Response('[{"id":"fresh"}]'))
      assert.deepEqual(await (await f.read(live)).json(), [{ id: 'fresh' }])
      t.mock.method(globalThis, 'fetch', async () => { throw new Error('offline') })
      assert.deepEqual(await (await f.read(ordinary)).json(), [{ id: 'fresh' }])
      await assert.rejects(f.read(live), /offline|no-response/)
      assert.equal(f.rows.size, 1, 'refreshes reuse the canonical offline key')
    }
  })
}

test('HTTP errors cannot replace a useful offline list', async t => {
  const f = fixture(t)
  const request = new Request('https://mobius.test/api/chats', { cache: 'no-store' })
  await f.cache.put(request, new Response('[{"id":"saved"}]'))
  t.mock.method(globalThis, 'fetch', async () => new Response('unavailable', { status: 503 }))
  assert.equal((await f.read(request)).status, 503)
  assert.deepEqual(await (await f.cache.match(request)).json(), [{ id: 'saved' }])
})

test('offline-write failure does not hide successful live evidence', async t => {
  const f = fixture(t)
  t.mock.method(f.cache, 'put', async () => { throw new Error('quota exhausted') })
  assert.equal((await f.read(new Request('https://mobius.test/api/apps/', { cache: 'no-store' }))).status, 200)
})

test('an aborted live request cannot replenish the offline snapshot', async t => {
  const f = fixture(t)
  const controller = new AbortController()
  t.mock.method(globalThis, 'fetch', async () => { controller.abort(); return new Response('[]') })
  await assert.rejects(f.read(new Request('https://mobius.test/api/chats', {
    cache: 'no-store', signal: controller.signal,
  })), error => error.name === 'AbortError')
  assert.equal(f.rows.size, 0)
})

test('a slow offline write cannot delay the live response or stream catch-up', { timeout: 2000 }, async t => {
  const f = fixture(t)
  let finishWrite
  t.mock.method(f.cache, 'put', () => new Promise(resolve => { finishWrite = resolve }))
  const [response, done] = new LiveShellList({ cacheName: SHELL_DATA_CACHE }).handleAll({
    request: new Request('https://mobius.test/api/chats', { cache: 'no-store' }),
    event: new ExtendableEvent(),
  })
  assert.equal((await response).status, 200)
  while (!finishWrite && !t.signal.aborted) await new Promise(resolve => setTimeout(resolve, 0))
  finishWrite(); await done
})
