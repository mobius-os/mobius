// Drive the commit-pinned proxy image lane through Workbox's CacheFirst lifecycle.
import { test } from 'node:test'
import assert from 'node:assert/strict'

globalThis.self = { location: { href: 'https://mobius.test/sw.js', origin: 'https://mobius.test' } }
globalThis.location = self.location
globalThis.caches = {}
Object.defineProperty(self, 'caches', { get: () => globalThis.caches })
globalThis.FetchEvent = class FetchEvent {}
globalThis.ExtendableEvent = class ExtendableEvent { waitUntil() {} }
const { pinnedProxyImageStrategy } = await import('../../sw-cache-strategies.js')
const { PROXY_PINNED_CACHE, PROXY_PINNED_MAX_ENTRIES } = await import('../../sw-cache-policy.js')

const COMMIT = '0123456789abcdef0123456789abcdef01234567'
const proxied = (name = 'icon.png') => 'https://mobius.test/api/proxy?url='
  + encodeURIComponent(`https://raw.githubusercontent.com/o/app/${COMMIT}/${name}`) + '&w=96'
const IMMUTABLE = 'private, max-age=31536000, immutable'

// Like CacheStorage, a stored response's Vary must match unless ignoreVary.
function fixture(t, { cacheControl = IMMUTABLE } = {}) {
  const rows = new Map()
  const deletes = []
  const url = request => typeof request === 'string' ? request : request.url
  const varies = (stored, request) => (stored.response.headers.get('vary') || '')
    .split(',').map(name => name.trim()).filter(Boolean)
    .some(name => stored.request.headers.get(name) !== request.headers.get(name))
  const cache = {
    match: async (request, options = {}) => {
      const stored = rows.get(url(request))
      if (!stored || (!options.ignoreVary && varies(stored, request))) return undefined
      return stored.response.clone()
    },
    put: async (request, response) => { rows.set(request.url, { request, response: response.clone() }) },
    keys: async () => [...rows.values()].map(row => row.request),
    delete: async (request, options = {}) => { deletes.push(options); return rows.delete(url(request)) },
  }
  const opened = []
  t.mock.property(globalThis, 'caches', {
    open: async name => { opened.push(name); return cache },
    match: (request, options) => cache.match(request, options),
  })
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response('webp', {
    headers: { 'cache-control': cacheControl, vary: 'Authorization' },
  }))
  const strategy = pinnedProxyImageStrategy()
  async function read(request) {
    const [response, done] = strategy.handleAll({ request, event: new ExtendableEvent() })
    await done
    return response
  }
  return { cache, rows, deletes, opened, fetch, read }
}

const request = (token, name) => new Request(proxied(name), {
  mode: 'cors', headers: { Authorization: `Bearer ${token}` },
})

test('a pinned image is fetched once, outside the HTTP cache, and reused by a renewed token', async t => {
  const f = fixture(t)
  assert.equal(await (await f.read(request('first'))).text(), 'webp')
  assert.equal(f.fetch.mock.callCount(), 1)
  // A PROXY_PINNED_CACHE bump must not be answered from an old HTTP-cache copy.
  assert.equal(f.fetch.mock.calls[0].arguments[1].cache, 'no-store')
  assert.deepEqual(new Set(f.opened), new Set([PROXY_PINNED_CACHE]))

  const renewed = request('renewed')
  assert.equal(await f.cache.match(renewed), undefined, 'Vary: Authorization alone would miss')
  assert.equal(await (await f.read(renewed)).text(), 'webp')
  assert.equal(f.fetch.mock.callCount(), 1)
})

test('an entry stored for the shell without CORS headers is readable by an opaque app frame', async t => {
  const f = fixture(t)
  await f.read(new Request(proxied(), { headers: { Authorization: 'Bearer shell' } }))
  assert.equal((await f.cache.match(new Request(proxied()), { ignoreVary: true }))
    .headers.get('access-control-allow-origin'), null)

  const hit = await f.read(request('app'))
  assert.equal(f.fetch.mock.callCount(), 1)
  assert.equal(hit.status, 200)
  assert.equal(hit.headers.get('access-control-allow-origin'), '*')
  assert.equal(await hit.text(), 'webp')
})

test('a response the server did not mark immutable is served but never pinned', async t => {
  const f = fixture(t, { cacheControl: 'private, max-age=299' })
  assert.equal(await (await f.read(request('one'))).text(), 'webp')
  assert.equal(f.rows.size, 0)
  await f.read(request('one'))
  assert.equal(f.fetch.mock.callCount(), 2)
})

test('the pinned cache keeps its newest entries, deleting the oldest whatever their Vary', async t => {
  const f = fixture(t)
  for (let i = 0; i < PROXY_PINNED_MAX_ENTRIES; i += 1) {
    const stored = request('old', `icon-${i}.png`)
    f.rows.set(stored.url, { request: stored, response: new Response('old', { headers: { vary: 'Authorization' } }) })
  }
  await f.read(request('new', 'newest.png'))
  assert.equal(f.rows.size, PROXY_PINNED_MAX_ENTRIES)
  assert.equal(f.rows.has(proxied('icon-0.png')), false)
  assert.equal(f.rows.has(proxied('newest.png')), true)
  assert.deepEqual(f.deletes, [{ ignoreVary: true }])
})
