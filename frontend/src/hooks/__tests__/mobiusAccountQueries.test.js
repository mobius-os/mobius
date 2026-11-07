/* Signing in to or out of the Möbius account, from any surface, refreshes the provider status and model list the picker reads. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import { QueryClient } from '@tanstack/react-query'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { setShimQueryClient } from '../../lib/__tests__/react-query-shim.mjs'
import { accountSummary, avatarKey, mobiusAccountQueries } from '../mobiusAccountQueries.js'
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

test('a handle saved from either the guide or Settings becomes the cached account', async t => {
  const queryClient = seeded()
  const sent = []
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    sent.push({ url: String(url), method: init.method, body: JSON.parse(init.body) })
    return Response.json({
      account_mode: 'linked',
      account_unavailable: false,
      profile: { user_id: 'u1', email: 'owner@example.test', handle: 'new_handle', avatar_url: null },
      deployments: [{ id: 'local', name: 'Test Möbius', status: 'Active', url: 'https://test.example', current: true }],
    })
  })
  await mobiusAccountQueries.saveHandle(queryClient, 'new_handle')
  assert.equal(sent.length, 1)
  assert.match(sent[0].url, /\/api\/identity\/profile$/)
  assert.equal(sent[0].method, 'PATCH')
  assert.deepEqual(sent[0].body, { handle: 'new_handle' })
  assert.equal(queryClient.getQueryData(mobiusAccountQueries.identity.key).profile.handle, 'new_handle')
})

const AVATAR_ADDRESS = 'https://photos.example/a/owner-1.png'
const accountWithPhoto = (address = AVATAR_ADDRESS) => ({
  account_mode: 'linked',
  account_unavailable: false,
  profile: { user_id: 'u1', email: 'owner@example.test', handle: 'owner', avatar_url: address },
  deployments: [{ id: 'local', name: 'Test Möbius', status: 'Active', url: 'https://test.example', current: true }],
})
const savedSummary = queryClient => queryClient.getQueryData(['identity-summary'])

test('the photo is keyed by a fingerprint of its address, which is never kept itself', () => {
  const key = avatarKey(accountWithPhoto())
  assert.match(key, /^avatar-[0-9a-z]+$/)
  assert.equal(avatarKey(accountWithPhoto()), key)
  assert.notEqual(avatarKey(accountWithPhoto('https://photos.example/a/owner-2.png')), key)
  assert.equal(avatarKey(accountWithPhoto(null)), '')
  assert.equal(avatarKey(null), '')
  const summary = accountSummary(accountWithPhoto())
  assert.equal(summary.avatar, key)
  assert.doesNotMatch(JSON.stringify(summary), /photos\.example|owner@example|Test Möbius/)
})

test('a handle saved from the guide also refreshes the saved Settings summary', async t => {
  const queryClient = seeded()
  t.mock.method(globalThis, 'fetch', async () => Response.json(accountWithPhoto()))
  await mobiusAccountQueries.saveHandle(queryClient, 'owner')
  assert.deepEqual(savedSummary(queryClient), {
    status: 'linked', mode: 'linked', handle: 'owner', avatar: avatarKey(accountWithPhoto()),
  })
})

test('any identity read refreshes the saved summary, with Settings not open', async t => {
  const queryClient = seeded()
  setShimQueryClient(queryClient)
  t.mock.method(globalThis, 'fetch', async () => Response.json(accountWithPhoto()))
  // The first-run guide reads the identity; nothing renders the Settings row.
  const view = renderHook(() => mobiusAccountQueries.identity.useQuery())
  t.after(() => view.unmount())
  for (let i = 0; i < 50 && !savedSummary(queryClient); i += 1) await tick(5)
  assert.equal(savedSummary(queryClient)?.handle, 'owner')
  assert.equal(savedSummary(queryClient).avatar, avatarKey(accountWithPhoto()))
})
