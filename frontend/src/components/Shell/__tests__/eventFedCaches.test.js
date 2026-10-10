import test from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/query-core'

import { invalidateEventFedCaches } from '../eventFedCaches.js'

const KEY = ['auth', 'providers', 'status']

function clientWith(updatedAt) {
  const client = new QueryClient()
  client.setQueryData(KEY, { mobius: { authenticated: true } }, { updatedAt })
  return client
}

test('first open refreshes provider status restored from a previous page load', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  await Promise.all(invalidateEventFedCaches(client))
  assert.equal(client.getQueryState(KEY).isInvalidated, true,
    'a persisted entry inside its staleTime would otherwise make no request at all')
})

test('first open reconciles provider status fetched before subscription', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt + 50)
  await Promise.all(invalidateEventFedCaches(client))
  assert.equal(client.getQueryState(KEY).isInvalidated, true,
    'a fetch before SSE subscription may precede an event with no replay')
})

test('first open reconciles a nonpersisted cache fetched before subscription', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  client.setQueryData(['models', 'registry'], [], { updatedAt: pageLoadedAt + 50 })
  await Promise.all(invalidateEventFedCaches(client))
  assert.equal(client.getQueryState(['models', 'registry']).isInvalidated, true)
})

test('remount reconciles status even if this page fetched it earlier', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt + 50)
  await Promise.all(invalidateEventFedCaches(client))
  client.setQueryData(KEY, { mobius: { authenticated: true } }, { updatedAt: pageLoadedAt + 100 })
  await Promise.all(invalidateEventFedCaches(client))
  assert.equal(client.getQueryState(KEY).isInvalidated, true,
    'an unmounted stream can miss events even without a reload')
})
