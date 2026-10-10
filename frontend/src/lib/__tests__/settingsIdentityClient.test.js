import { afterEach, beforeEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, QueryObserver } from '@tanstack/react-query'

import {
  IDENTITY_KEY,
  identityRequest,
  disconnectIdentity,
  loadIdentity,
  publishIdentity,
  retryDeploymentDeletion,
} from '../../components/SettingsView/identity/identity-client.js'
import { deploymentPresentation } from '../../components/SettingsView/identity/identity-contract.js'

const realFetch = globalThis.fetch

const deletionTarget = {
  id: 'mob_example', name: 'Example', status: 'delete_failed', url: null,
  railway_url: 'https://railway.com/project/example', current_step: null, last_error: null,
  resources: { cpu: null, memory_mb: null, volume_size_mb: null, plan: 'hobby' },
  actions: { edit_resources: false, retry: true, delete: true },
}
const deletionInventory = target => ({
  railway_access: 'available',
  connection: { connected: true, account: 'owner', workspace: 'workspace', plan: 'hobby', deploy_blocked: '' },
  instances: target ? [target] : [],
})

test('ambiguous deletion copy requires Railway reconciliation rather than blind retry', () => {
  for (const last_error of [null, 'Check the build logs']) {
    const presentation = deploymentPresentation({ ...deletionTarget, last_error })
    assert.match(presentation.detail, /Check Railway first/)
    assert.match(presentation.detail, /same project still exists and is eligible/)
    assert.doesNotMatch(presentation.detail, /Try deleting again/)
  }
})

test('deletion retry revalidates inventory and Railway presence before the mutation callback', async () => {
  serve(
    { body: deletionInventory(deletionTarget) },
    { body: { state: 'present', message: 'Project still exists.', can_confirm_absent: false } },
  )
  let retried = false
  await retryDeploymentDeletion('token', deletionTarget, async id => {
    assert.equal(id, deletionTarget.id)
    assert.deepEqual(calls.map(call => call.url), [
      '/api/identity/railway', '/api/identity/railway/deployments/mob_example/deletion',
    ])
    retried = true
  })
  assert.equal(retried, true)
})

for (const state of ['missing', 'missing_unconfirmed', 'authorization', 'unknown']) {
  test(`deletion retry does not mutate after a ${state} reconciliation`, async () => {
    serve(
      { body: deletionInventory(deletionTarget) },
      { body: { state, message: 'Check Railway.', can_confirm_absent: state.startsWith('missing') } },
    )
    await assert.rejects(retryDeploymentDeletion('token', deletionTarget, () => assert.fail('unsafe retry')), /deletion was not retried/)
  })
}

for (const target of [null,
  { ...deletionTarget, id: 'mob_other' },
  { ...deletionTarget, status: 'deleting' },
  { ...deletionTarget, actions: { ...deletionTarget.actions, retry: false } },
  { ...deletionTarget, railway_url: 'https://railway.com/project/replacement' },
  { ...deletionTarget, railway_url: null },
]) {
  test(`deletion retry rejects a stale or ineligible target (${JSON.stringify(target)})`, async () => {
    serve({ body: deletionInventory(target) })
    await assert.rejects(retryDeploymentDeletion('token', deletionTarget, () => assert.fail('unsafe retry')), /no longer eligible/)
    assert.equal(calls.length, 1)
  })
}

for (const failure of [
  { status: 404, body: { detail: 'Check unavailable' } },
  { status: 503, body: { detail: 'Check unavailable' } },
  { body: { state: 'present', message: 'Malformed diagnosis', can_confirm_absent: true } },
  new TypeError('network down'),
]) {
  test(`deletion retry fails closed when reconciliation fails (${JSON.stringify(failure)})`, async () => {
    serve({ body: deletionInventory(deletionTarget) }, failure)
    await assert.rejects(retryDeploymentDeletion('token', deletionTarget, () => assert.fail('unsafe retry')))
  })
}

for (const inventory of [
  { ...deletionInventory(deletionTarget), connection: { ...deletionInventory(deletionTarget).connection, connected: false } },
  { railway_access: 'unavailable', connection: null, instances: [] },
  { ...deletionInventory(deletionTarget), instances: [{ id: deletionTarget.id }] },
]) {
  test(`deletion retry fails closed on unavailable or malformed inventory (${JSON.stringify(inventory)})`, async () => {
    serve({ body: inventory })
    await assert.rejects(retryDeploymentDeletion('token', deletionTarget, () => assert.fail('unsafe retry')))
    assert.equal(calls.length, 1)
  })
}

test('deletion retry rejects an original target without a Railway project identity', async () => {
  serve({ body: deletionInventory(deletionTarget) })
  await assert.rejects(retryDeploymentDeletion('token', { ...deletionTarget, railway_url: null }, () => assert.fail('unsafe retry')), /no longer eligible/)
  assert.equal(calls.length, 1)
})
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


const flush = () => new Promise(resolve => setImmediate(resolve))
const json = (body, status = 200) => new Response(JSON.stringify(body), { status })

function observeIdentity() {
  const observer = new QueryObserver(queryClient, {
    queryKey: IDENTITY_KEY,
    queryFn: ({ signal }) => identityRequest('token', '', { signal }),
    staleTime: Infinity,
    retry: false,
  })
  const unsubscribe = observer.subscribe(() => {})
  return { observer, unsubscribe }
}

test('queried Railway reads validate nested state without changing the transport URL', async () => {
  const payload = {
    railway_access: 'available',
    connection: { connected: true, account: 'owner@example.com', workspace: 'Example',
      plan: 'Hobby', deploy_blocked: '', plan_limits: { cpu: 'bad' }, adopt_current: true },
    instances: [],
  }
  serve({ body: payload })
  const parsed = await identityRequest('token', '/railway?region_options=1')
  assert.equal(calls[0].url, '/api/identity/railway?region_options=1')
  assert.equal(parsed.connection.plan_limits, undefined)
  assert.equal(parsed.connection.adopt_current, undefined)
  serve({ body: { ...payload, instances: [{ id: 'bad', url: 'javascript:alert(1)' }] } })
  await assert.rejects(identityRequest('token', '/railway?region_options=1'), /invalid Railway/)
})

test('confirmed unlink clears linked details and avatar even when the next identity read fails', async () => {
  queryClient.setQueryData(IDENTITY_KEY, identity('sample'))
  const avatarKey = [...IDENTITY_KEY, 'avatar', 'https://photos.example/old']
  queryClient.setQueryData(avatarKey, 'old-blob')
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return options?.method === 'DELETE'
      ? new Response(null, { status: 204 })
      : json({ detail: 'Account service unavailable' }, 503)
  }
  const watching = observeIdentity()
  try {
    await disconnectIdentity(queryClient, 'token')
    await flush()
    assert.equal(queryClient.getQueryData(IDENTITY_KEY), undefined)
    assert.equal(queryClient.getQueryData(avatarKey), undefined)
    assert.match(queryClient.getQueryState(IDENTITY_KEY).error.message, /unavailable/)
    assert.equal(calls[0].options.method, 'DELETE')
    assert.equal(calls[1].url, '/api/identity')
  } finally { watching.unsubscribe() }
})

test('confirmed unlink aborts and fences a late pre-delete identity read', async () => {
  queryClient.setQueryData(IDENTITY_KEY, identity('sample'))
  let finishOld
  let oldSignal
  let reads = 0
  globalThis.fetch = async (url, options) => {
    if (options?.method === 'DELETE') return new Response(null, { status: 204 })
    if (++reads === 1) {
      oldSignal = options.signal
      return new Promise(resolve => { finishOld = resolve })
    }
    return json({ detail: 'Refresh unavailable' }, 503)
  }
  const watching = observeIdentity()
  try {
    const oldRead = watching.observer.refetch()
    await flush()
    await disconnectIdentity(queryClient, 'token')
    await flush()
    assert.equal(oldSignal.aborted, true)
    finishOld(json(identity('obsolete')))
    await oldRead
    await flush()
    assert.equal(queryClient.getQueryData(IDENTITY_KEY), undefined)
  } finally { watching.unsubscribe() }
})

test('a rejected unlink preserves the confirmed linked identity', async () => {
  const previous = identity('sample')
  queryClient.setQueryData(IDENTITY_KEY, previous)
  serve({ status: 502, body: { detail: 'Revocation was not confirmed' } })
  await assert.rejects(disconnectIdentity(queryClient, 'token'), /not confirmed/)
  assert.deepEqual(queryClient.getQueryData(IDENTITY_KEY), previous)
})

test('unlink waits for the complete backend identity rather than inventing an empty signed-out payload', async () => {
  queryClient.setQueryData(IDENTITY_KEY, identity('sample'))
  const localIdentity = { ...identity('sample'), account_mode: 'signed_out', profile: null }
  globalThis.fetch = async (_url, options) => options?.method === 'DELETE'
    ? new Response(null, { status: 204 }) : json(localIdentity)
  const watching = observeIdentity()
  try {
    await disconnectIdentity(queryClient, 'token')
    await flush()
    assert.deepEqual(queryClient.getQueryData(IDENTITY_KEY), localIdentity)
    assert.equal(queryClient.getQueryData(IDENTITY_KEY).deployments[0].current, true)
  } finally { watching.unsubscribe() }
})

test('reconnection supersedes the post-unlink refresh before publishing the new linked identity', async () => {
  queryClient.setQueryData(IDENTITY_KEY, identity('sample'))
  let finishRefresh
  let refreshSignal
  globalThis.fetch = async (_url, options) => {
    if (options?.method === 'DELETE') return new Response(null, { status: 204 })
    refreshSignal = options.signal
    return new Promise(resolve => { finishRefresh = resolve })
  }
  const watching = observeIdentity()
  try {
    await disconnectIdentity(queryClient, 'token')
    await flush()
    publishIdentity(queryClient, identity('reconnected'))
    assert.equal(refreshSignal.aborted, true)
    finishRefresh(json({ ...identity('old'), account_mode: 'signed_out', profile: null }))
    await flush()
    assert.equal(profileOf().handle, 'reconnected')
  } finally { watching.unsubscribe() }
})
