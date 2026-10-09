import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/react-query'

import {
  IDENTITY_KEY,
  identityRequest,
  loadIdentity,
  publishIdentity,
} from '../../components/SettingsView/identity/identity-client.js'

const realFetch = globalThis.fetch
let calls
let queryClient

function identity(handle, avatarUrl = null) {
  return {
    account_mode: 'linked',
    account_unavailable: false,
    instance_id: null,
    member_since: null,
    profile: {
      user_id: 'user-1',
      email: 'owner@example.com',
      display_name: 'Sam Example',
      handle,
      avatar_url: avatarUrl,
    },
    deployments: [{ id: 'dep-1', name: 'My Möbius', status: 'active', url: 'https://mobius.example', current: true }],
  }
}

function serve(...responses) {
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    const next = responses.length > 1 ? responses.shift() : responses[0]
    if (next instanceof Error) throw next
    return new Response(JSON.stringify(next.body), {
      status: next.status || 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
}

const profileOf = () => queryClient.getQueryData(IDENTITY_KEY)?.profile

beforeEach(() => {
  calls = []
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

afterEach(() => {
  globalThis.fetch = realFetch
  queryClient.clear()
})

test('opening the account page right after the overview reuses one identity read', async () => {
  serve({ body: identity('sample') })
  await loadIdentity(queryClient, 'token')
  await loadIdentity(queryClient, 'token')
  assert.equal(calls.length, 1)
  assert.equal(profileOf().handle, 'sample')
})

test('overlapping overview and account reads share the in-flight request', async () => {
  serve({ body: identity('sample') })
  await Promise.all([loadIdentity(queryClient, 'token'), loadIdentity(queryClient, 'token')])
  assert.equal(calls.length, 1)
})

test('focus and Try again still ask the identity service again', async () => {
  serve({ body: identity('before') }, { body: identity('after') })
  await loadIdentity(queryClient, 'token')
  await loadIdentity(queryClient, 'token', { force: true })
  assert.equal(calls.length, 2)
  assert.equal(profileOf().handle, 'after')
})

test('a saved handle is what the overview row reads next, without another request', async () => {
  serve({ body: identity('before') })
  await loadIdentity(queryClient, 'token')
  publishIdentity(queryClient, identity('after'))
  await loadIdentity(queryClient, 'token')
  assert.equal(calls.length, 1)
  assert.equal(profileOf().handle, 'after')
})

test('an uploaded photo drops the cached photo so both rows show the new one', () => {
  queryClient.setQueryData([...IDENTITY_KEY, 'avatar', 'https://photos.example/old'], 'old-blob')
  publishIdentity(queryClient, identity('sample', 'https://photos.example/new'), { avatarChanged: true })
  assert.equal(queryClient.getQueryData([...IDENTITY_KEY, 'avatar', 'https://photos.example/old']), undefined)
  assert.equal(profileOf().avatar_url, 'https://photos.example/new')
})

test('an unreachable identity service keeps the last confirmed profile and explains why', async () => {
  serve({ body: identity('sample') }, new TypeError('network down'))
  await loadIdentity(queryClient, 'token')
  await assert.rejects(
    loadIdentity(queryClient, 'token', { force: true }),
    /could not reach its identity service/,
  )
  assert.equal(profileOf().handle, 'sample')
  assert.match(queryClient.getQueryState(IDENTITY_KEY).error.message, /could not reach/)
})

test('an identity service error carries its own message and code', async () => {
  serve({ status: 503, body: { detail: { message: 'Account service is down.', code: 'unavailable' } } })
  await assert.rejects(identityRequest('token'), error => {
    assert.equal(error.message, 'Account service is down.')
    assert.equal(error.status, 503)
    assert.equal(error.code, 'unavailable')
    return true
  })
  assert.equal(calls[0].options.headers.Authorization, 'Bearer token')
})
