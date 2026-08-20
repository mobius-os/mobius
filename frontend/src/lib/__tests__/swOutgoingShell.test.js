import test from 'node:test'
import assert from 'node:assert/strict'
import {
  findOutgoingShellEntries,
  refreshOutgoingShellEntries,
} from '../swOutgoingShell.js'

function cacheStorage(seed) {
  const stores = new Map(Object.entries(seed).map(([name, urls]) => [
    name,
    new Map(urls.map(url => [url, new Response(`old:${url}`)])),
  ]))
  return {
    stores,
    async keys() { return [...stores.keys()] },
    async open(name) {
      const store = stores.get(name)
      return {
        async keys() { return [...store.keys()].map(url => new Request(url)) },
        async put(request, response) { store.set(request.url, response) },
      }
    },
  }
}

test('finds only revisioned shell documents across outgoing caches', async () => {
  const storage = cacheStorage({
    precache: [
      'https://mobius.test/index.html?__WB_REVISION__=old',
      'https://mobius.test/assets/old.js',
    ],
    runtime: ['https://mobius.test/api/chats'],
  })

  const entries = await findOutgoingShellEntries(storage)
  assert.deepEqual(entries.map(({ cacheName, request }) => [cacheName, request.url]), [[
    'precache',
    'https://mobius.test/index.html?__WB_REVISION__=old',
  ]])
})

test('replaces outgoing shell responses while preserving their revisioned keys', async () => {
  const oldKey = 'https://mobius.test/index.html?__WB_REVISION__=old'
  const storage = cacheStorage({ precache: [oldKey] })
  const entries = await findOutgoingShellEntries(storage)

  await refreshOutgoingShellEntries(storage, entries, new Response('current shell'))

  assert.equal(await storage.stores.get('precache').get(oldKey).text(), 'current shell')
})

test('leaves outgoing caches untouched when the current document fetch failed', async () => {
  const oldKey = 'https://mobius.test/index.html?__WB_REVISION__=old'
  const storage = cacheStorage({ precache: [oldKey] })
  const entries = await findOutgoingShellEntries(storage)

  await refreshOutgoingShellEntries(storage, entries, new Response('nope', { status: 503 }))

  assert.equal(await storage.stores.get('precache').get(oldKey).text(), `old:${oldKey}`)
})
