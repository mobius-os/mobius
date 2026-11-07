/* Signing in to or out of the Möbius account, from any surface, refreshes the provider status and model list the picker reads. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { mobiusAccountQueries } from '../mobiusAccountQueries.js'
import { authQueries, modelQueries } from '../queries.js'

function seeded() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  queryClient.setQueryData(authQueries.provider.statuses.key, { mobius: { configured: false } })
  queryClient.setQueryData(modelQueries.registry.key, { models: [] })
  queryClient.setQueryData(mobiusAccountQueries.railway.key, { instances: [] })
  queryClient.setQueryData(mobiusAccountQueries.agent.key, { trial: null })
  return queryClient
}

const stale = (queryClient, key) => queryClient.getQueryState(key).isInvalidated

test('a sign-in shows the new identity and marks provider status and models for a fresh read', async () => {
  const queryClient = seeded()
  const identity = { account_mode: 'linked', profile: { handle: 'owner' } }
  await mobiusAccountQueries.signedIn(queryClient, identity)
  assert.deepEqual(queryClient.getQueryData(mobiusAccountQueries.identity.key), identity)
  assert.equal(stale(queryClient, mobiusAccountQueries.identity.key), false)
  assert.equal(stale(queryClient, authQueries.provider.statuses.key), true)
  assert.equal(stale(queryClient, modelQueries.registry.key), true)
  assert.equal(stale(queryClient, mobiusAccountQueries.agent.key), true)
})

test('a sign-out forgets the account deployments and refreshes provider status and models', async () => {
  const queryClient = seeded()
  await mobiusAccountQueries.signedOut(queryClient)
  assert.equal(queryClient.getQueryState(mobiusAccountQueries.railway.key), undefined)
  assert.equal(queryClient.getQueryState(mobiusAccountQueries.agent.key), undefined)
  assert.equal(stale(queryClient, authQueries.provider.statuses.key), true)
  assert.equal(stale(queryClient, modelQueries.registry.key), true)
})
