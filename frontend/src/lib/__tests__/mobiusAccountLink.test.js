/* Signing in to a Möbius account from the top-level shell: popup, validated completion message, lost-response-safe completion and cleanup on every terminal path. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import {
  AccountLinkError,
  completeAccountLink,
  linkMobiusAccount,
  parseLinkAttempt,
} from '../mobiusAccountLink.js'
import { MobiusAccessError } from '../mobiusAccount.js'

const ACCOUNT_ORIGIN = 'https://www.mobius.you'
const STATE = 's'.repeat(40)
const CODE = 'c'.repeat(40)
const ATTEMPT = 'a'.repeat(24)
const start = {
  authorization_url: `${ACCOUNT_ORIGIN}/connect/mobius?state=${STATE}`,
  attempt: ATTEMPT,
  state: STATE,
  expires_at: '2030-01-01T00:00:00Z',
}
const linked = { account_mode: 'linked', account_unavailable: false, profile: { handle: 'ada' } }
const signedOut = { account_mode: 'signed_out', account_unavailable: false, profile: null }

function fakePopup() {
  return {
    closed: false,
    navigatedTo: null,
    closeCalls: 0,
    location: { replace(url) { this.owner.navigatedTo = url } },
    close() { this.closeCalls += 1; this.closed = true },
    init() { this.location.owner = this; return this },
  }.init()
}

function fakeWindow() {
  const listeners = new Set()
  return {
    addEventListener(type, fn) { if (type === 'message') listeners.add(fn) },
    removeEventListener(type, fn) { if (type === 'message') listeners.delete(fn) },
    post(origin, data) { for (const fn of [...listeners]) fn({ origin, data }) },
    get listening() { return listeners.size },
  }
}

function fakeRequest(routes) {
  const calls = []
  const request = async (path, options = {}) => {
    calls.push({ path, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : undefined })
    const handler = routes[path]
    if (!handler) throw new Error(`unexpected request ${path}`)
    return typeof handler === 'function' ? handler() : handler
  }
  return { request, calls }
}

function setup(routes = {}) {
  const popup = fakePopup()
  const target = fakeWindow()
  const { request, calls } = fakeRequest({
    '/identity/link/start': start,
    '/identity/link/complete': linked,
    ...routes,
  })
  return { popup, target, request, calls, opened: () => popup }
}

const completion = (target, data = { type: 'mobius-account-link', code: CODE, state: STATE }) => target.post(ACCOUNT_ORIGIN, data)

test('a blocked sign-in window stops before any link attempt is created', async () => {
  const { request, calls, target } = setup()
  await assert.rejects(
    linkMobiusAccount({ provider: 'google', openWindow: () => null, request, eventTarget: target }),
    error => error instanceof AccountLinkError && error.reason === 'blocked',
  )
  assert.deepEqual(calls, [])
  assert.equal(target.listening, 0)
})

test('the validated completion message finishes the link with code, state and attempt', async () => {
  const { popup, target, request, calls, opened } = setup()
  let completing = 0
  const run = linkMobiusAccount({ provider: 'apple', openWindow: opened, request, eventTarget: target, onCompleting: () => { completing += 1 }, closedPollMs: 5 })
  await tick(5)
  assert.equal(popup.navigatedTo, start.authorization_url)
  assert.deepEqual(calls[0], { path: '/identity/link/start', method: 'POST', body: { provider: 'apple' } })
  completion(target)
  const outcome = await run
  assert.deepEqual(outcome, { status: 'linked', identity: linked })
  assert.equal(completing, 1)
  assert.deepEqual(calls[1], { path: '/identity/link/complete', method: 'POST', body: { code: CODE, state: STATE, attempt: ATTEMPT } })
  assert.ok(popup.closeCalls >= 1)
  assert.equal(target.listening, 0)
})

test('messages from another origin, for another state or with extra fields are ignored', async () => {
  const { target, request, calls, opened } = setup()
  const run = linkMobiusAccount({ provider: 'google', openWindow: opened, request, eventTarget: target, closedPollMs: 5 })
  await tick(5)
  target.post('https://evil.example', { type: 'mobius-account-link', code: CODE, state: STATE })
  completion(target, { type: 'mobius-account-link', code: CODE, state: 't'.repeat(40) })
  completion(target, { type: 'mobius-account-link', code: CODE, state: STATE, extra: 1 })
  completion(target, { type: 'moebius:account-link-result', code: CODE, state: STATE })
  await tick(5)
  assert.equal(calls.length, 1, 'nothing completed yet')
  completion(target)
  assert.equal((await run).status, 'linked')
})

test('closing the sign-in window ends the attempt and removes the listener', async () => {
  const { popup, target, request, calls, opened } = setup()
  const run = linkMobiusAccount({ provider: 'google', openWindow: opened, request, eventTarget: target, closedPollMs: 5 })
  await tick(5)
  popup.closed = true
  await assert.rejects(run, error => error.reason === 'closed')
  assert.equal(target.listening, 0)
  assert.equal(calls.length, 1)
})

test('cancelling while waiting closes the popup and never completes', async () => {
  const { popup, target, request, calls, opened } = setup()
  const controller = new AbortController()
  const run = linkMobiusAccount({ provider: 'google', signal: controller.signal, openWindow: opened, request, eventTarget: target, closedPollMs: 5 })
  await tick(5)
  controller.abort()
  await assert.rejects(run, error => error.reason === 'cancelled')
  completion(target)
  await tick(5)
  assert.equal(calls.length, 1)
  assert.ok(popup.closeCalls >= 1)
  assert.equal(target.listening, 0)
})

test('an attempt that outlives its local lifetime times out and cleans up', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] })
  const { popup, target, request, opened } = setup()
  let clock = 0
  const run = linkMobiusAccount({ provider: 'google', openWindow: opened, request, eventTarget: target, now: () => clock })
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve()
  clock = 10 * 60 * 1000
  t.mock.timers.tick(10 * 60 * 1000)
  await assert.rejects(run, error => error.reason === 'timeout')
  assert.ok(popup.closeCalls >= 1)
  assert.equal(target.listening, 0)
})

test('a refused link start closes the popup and reports the service message', async () => {
  const { popup, target, request, calls, opened } = setup({
    '/identity/link/start': () => { throw new MobiusAccessError('This Möbius is already linked.', 409) },
  })
  await assert.rejects(
    linkMobiusAccount({ provider: 'google', openWindow: opened, request, eventTarget: target }),
    /already linked/,
  )
  assert.equal(popup.closeCalls, 1)
  assert.equal(target.listening, 0)
  assert.equal(calls.length, 1)
})

test('cancelling after the account approved does not abandon completion', async () => {
  let release
  const { target, request, opened, calls } = setup({
    '/identity/link/complete': () => new Promise(resolve => { release = () => resolve(linked) }),
  })
  const controller = new AbortController()
  const run = linkMobiusAccount({ provider: 'google', signal: controller.signal, openWindow: opened, request, eventTarget: target, closedPollMs: 5 })
  await tick(5)
  completion(target)
  await tick(5)
  controller.abort()
  release()
  assert.equal((await run).status, 'linked')
  assert.equal(calls.at(-1).path, '/identity/link/complete')
})

test('a lost completion response is resolved by re-reading the account before any retry', async () => {
  const { request, calls } = fakeRequest({
    '/identity/link/complete': () => { throw new MobiusAccessError('offline', 0) },
    '/identity': linked,
  })
  const payload = { code: CODE, state: STATE, attempt: ATTEMPT }
  assert.deepEqual(await completeAccountLink(payload, { request }), { status: 'linked', identity: linked })
  assert.deepEqual(calls.map(call => call.path), ['/identity/link/complete', '/identity'])
})

test('an unconfirmed completion keeps the approval for a retry only when retrying can help', async () => {
  const payload = { code: CODE, state: STATE, attempt: ATTEMPT }
  const unconfirmed = fakeRequest({
    '/identity/link/complete': () => { throw new MobiusAccessError('exchange unavailable', 502) },
    '/identity': signedOut,
  })
  const outcome = await completeAccountLink(payload, { request: unconfirmed.request })
  assert.equal(outcome.status, 'unconfirmed')
  assert.deepEqual(outcome.completion, payload)

  const refused = fakeRequest({
    '/identity/link/complete': () => { throw new MobiusAccessError('That sign-in expired.', 400) },
    '/identity': signedOut,
  })
  await assert.rejects(completeAccountLink(payload, { request: refused.request }), /expired/)
})

test('a link attempt must name its state in an https or loopback authorization address', () => {
  assert.equal(parseLinkAttempt(start).authorizationOrigin, ACCOUNT_ORIGIN)
  assert.throws(() => parseLinkAttempt({ ...start, authorization_url: `http://www.mobius.you/connect?state=${STATE}` }), /invalid sign-in address/)
  assert.throws(() => parseLinkAttempt({ ...start, authorization_url: `${ACCOUNT_ORIGIN}/connect?state=other${STATE}` }), /invalid sign-in address/)
  assert.throws(() => parseLinkAttempt({ ...start, authorization_url: `${ACCOUNT_ORIGIN}/connect?state=${STATE}#x` }), /invalid sign-in address/)
  assert.throws(() => parseLinkAttempt({ ...start, state: 'short' }), /invalid sign-in attempt/)
  assert.equal(parseLinkAttempt({ ...start, authorization_url: `http://localhost:8787/connect?state=${STATE}` }).authorizationOrigin, 'http://localhost:8787')
})
