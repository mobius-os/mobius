/* Settings GitHub connection: status normalization and the server-paced sign-in wait. */
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'

import {
  fetchGithubStatus,
  githubStatus,
  hasFullPrAccess,
  hasPrivateRepoAccess,
  startGithubSignIn,
  unknownGithubStatus,
  waitForGithubSignIn,
} from '../githubConnection.js'

const originalFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = originalFetch })

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
})

function stubFetch(...responses) {
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ path: String(url).replace(/^.*\/api/, ''), body: init.body ? JSON.parse(init.body) : null })
    const next = responses.shift()
    if (next instanceof Error) throw next
    return typeof next === 'function' ? next() : next
  }
  return calls
}

test('scope helpers match the backend PR-access contract', () => {
  assert.equal(hasFullPrAccess(['public_repo']), false)
  assert.equal(hasFullPrAccess(['public_repo', 'workflow']), true)
  assert.equal(hasFullPrAccess(['repo', 'workflow']), true)
  assert.equal(hasPrivateRepoAccess(['public_repo', 'workflow']), false)
  assert.equal(hasPrivateRepoAccess(['repo', 'workflow']), true)
})

test('status carries the account and a resumable sign-in', async () => {
  stubFetch(json({
    connected: false,
    device_flow_available: true,
    active_attempt: { attempt_id: 'a1', user_code: 'WXYZ-1234', verification_uri: 'https://github.com/login/device' },
  }))
  assert.deepEqual(await fetchGithubStatus(), {
    state: 'disconnected', login: '', scopes: [], signInAvailable: true,
    attempt: { attemptId: 'a1', userCode: 'WXYZ-1234', verificationUri: 'https://github.com/login/device' },
  })
  stubFetch(json({ connected: true, login: 'octo', scopes: ['repo', 'workflow'] }))
  const connected = await fetchGithubStatus()
  assert.equal(connected.state, 'connected')
  assert.equal(connected.login, 'octo')
})

test('a read caches only the account, and a failed read leaves the last confirmed one untouched', async () => {
  const queryClient = new QueryClient()
  stubFetch(json({
    connected: true, login: 'octo', scopes: ['repo', 'workflow'], device_flow_available: true,
    active_attempt: { attempt_id: 'a1', user_code: 'WXYZ-1234', verification_uri: 'https://github.com/login/device' },
  }))
  assert.equal((await githubStatus.refresh(queryClient)).attempt.userCode, 'WXYZ-1234')
  const account = { state: 'connected', login: 'octo', scopes: ['repo', 'workflow'], signInAvailable: true }
  assert.deepEqual(queryClient.getQueryData(githubStatus.key), account)
  stubFetch(new TypeError('offline'))
  await assert.rejects(githubStatus.refresh(queryClient))
  stubFetch(json({ detail: 'Backend restarting' }, 503))
  await assert.rejects(githubStatus.refresh(queryClient), /Backend restarting/)
  assert.deepEqual(queryClient.getQueryData(githubStatus.key), account)
  assert.deepEqual(unknownGithubStatus(new Error('Backend restarting')), { state: 'unknown', message: 'Backend restarting' })
})

test('sign-in sends the private-repo choice and polls the exact attempt until complete', async () => {
  const calls = stubFetch(
    json({ attempt_id: 'a9', user_code: 'CODE-9999', verification_uri: 'https://github.com/login/device' }),
    json({ attempt_id: 'a9', status: 'pending', retry_after: 0 }),
    json({ attempt_id: 'a9', status: 'complete', login: 'octo' }),
  )
  const attempt = await startGithubSignIn({ privateRepos: true })
  assert.equal(attempt.userCode, 'CODE-9999')
  assert.deepEqual(await waitForGithubSignIn(attempt.attemptId), { status: 'complete' })
  assert.deepEqual(calls.map(c => c.path), ['/github/connect/start', '/github/connect/poll', '/github/connect/poll'])
  assert.deepEqual(calls[0].body, { private_repos: true })
  assert.deepEqual(calls[1].body, { attempt_id: 'a9' })
})

test('server outcomes stay distinct and a lost attempt fails without retrying', async () => {
  stubFetch(json({ attempt_id: 'a1', status: 'failed', reason: 'access_denied' }))
  assert.deepEqual(await waitForGithubSignIn('a1'), { status: 'failed', message: 'GitHub sign-in was denied.' })
  stubFetch(json({ attempt_id: 'a1', status: 'expired', reason: 'expired_token' }))
  assert.equal((await waitForGithubSignIn('a1')).message, 'The code expired. Please try again.')
  const calls = stubFetch(json({ detail: 'This GitHub connection attempt no longer exists.' }, 404))
  assert.deepEqual(await waitForGithubSignIn('gone'), {
    status: 'failed', message: 'This GitHub connection attempt no longer exists.',
  })
  assert.equal(calls.length, 1)
})

test('aborting the wait stops polling', async () => {
  const controller = new AbortController()
  const calls = stubFetch(() => { controller.abort(); return json({ status: 'pending', retry_after: 5 }) })
  assert.deepEqual(await waitForGithubSignIn('a1', { signal: controller.signal }), { status: 'cancelled' })
  assert.equal(calls.length, 1)
})


test('each completed polling delay releases its abort listener', async () => {
  const controller = new AbortController()
  let count = 0
  const add = controller.signal.addEventListener.bind(controller.signal)
  const remove = controller.signal.removeEventListener.bind(controller.signal)
  controller.signal.addEventListener = (...args) => { count += 1; add(...args) }
  controller.signal.removeEventListener = (...args) => { count -= 1; remove(...args) }
  stubFetch(json({ status: 'complete' }))
  await waitForGithubSignIn('a1', { signal: controller.signal })
  assert.equal(count, 0)
})
