import { test } from 'node:test'
import assert from 'node:assert/strict'

import { serveShellNavigation } from '../swShellNavigation.js'

test('online navigation always returns the current server document', async () => {
  const fresh = { ok: true, generation: 'current' }
  let precacheReads = 0
  const result = await serveShellNavigation({
    request: { url: '/shell/' },
    fetchFresh: async () => fresh,
    matchPrecache: async () => { precacheReads += 1 },
    errorResponse: () => ({ error: true }),
  })
  assert.equal(result, fresh)
  assert.equal(precacheReads, 0)
})

test('offline navigation falls back to the worker-matched shell generation', async () => {
  const cached = { ok: true, generation: 'worker-precache' }
  const keys = []
  const result = await serveShellNavigation({
    request: { url: '/shell/' },
    fetchFresh: async () => { throw new Error('offline') },
    matchPrecache: async key => { keys.push(key); return key === '/index.html' ? cached : null },
    errorResponse: () => ({ error: true }),
  })
  assert.equal(result, cached)
  assert.deepEqual(keys, ['/index.html'])
})

test('a failed server response never replaces the coherent offline fallback', async () => {
  const cached = { ok: true, generation: 'worker-precache' }
  const result = await serveShellNavigation({
    request: { url: '/shell/' },
    fetchFresh: async () => ({ ok: false, status: 503 }),
    matchPrecache: async key => key === '/index.html' ? cached : null,
    errorResponse: () => ({ error: true }),
  })
  assert.equal(result, cached)
})
