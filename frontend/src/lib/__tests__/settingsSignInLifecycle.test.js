import { test } from 'node:test'
import assert from 'node:assert/strict'
import { SignInModal } from '../../components/SettingsView/identity/IdentityAccount.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const flush = () => new Promise(resolve => setImmediate(resolve))
const state = 's'.repeat(32)
const identity = {
  account_mode: 'linked', account_unavailable: false, instance_id: null, member_since: null,
  profile: { user_id: 'user-example', email: 'user@example.com', display_name: 'Example', handle: 'example', avatar_url: null },
  deployments: [{ id: 'dep-example', name: 'Example', status: 'active', url: 'https://instance.example', current: true }],
}
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
function find(node, className) {
  if (node?.props?.className === className) return node
  for (const child of [node?.props?.children].flat(Infinity)) {
    const match = child && typeof child === 'object' && find(child, className)
    if (match) return match
  }
}
function setup(t) {
  const oldWindow = globalThis.window
  const oldDocument = globalThis.document
  const listeners = new Set()
  const calls = []
  const signedIn = []
  const popup = { closed: false, close() {}, location: { replace() {} } }
  globalThis.document = { activeElement: null }
  globalThis.window = {
    open: () => popup,
    addEventListener: (_, fn) => listeners.add(fn),
    removeEventListener: (_, fn) => listeners.delete(fn),
  }
  const post = deferred()
  const reconciliation = deferred()
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url, options })
    if (url.endsWith('/link/start')) return Promise.resolve(response({
      state, attempt: 'a'.repeat(16), expires_at: '2030-01-01T00:00:00Z',
      authorization_url: `https://account.example/link?state=${state}`,
    }))
    return url.endsWith('/link/complete') ? post.promise : reconciliation.promise
  })
  const view = renderHook(props => SignInModal(props), { token: 'mock-token', onClose() {}, onSignedIn: value => signedIn.push(value) })
  t.after(() => {
    view.unmount()
    if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow
    if (oldDocument === undefined) delete globalThis.document; else globalThis.document = oldDocument
  })
  const start = async () => {
    const running = find(view.result.current, 'id-provider').props.onClick()
    await flush()
    for (const listener of listeners) listener({ source: popup, origin: 'https://account.example', data: { type: 'mobius-account-link', state, code: 'c'.repeat(32) } })
    await flush()
    return { running }
  }
  return { view, start, post, reconciliation, calls, signedIn }
}

test('unmount fences a delayed successful sign-in completion', async t => {
  const s = setup(t)
  const { running } = await s.start()
  s.view.unmount()
  s.post.resolve(response(identity))
  await running
  assert.equal(s.calls[1].options.signal.aborted, true)
  assert.deepEqual(s.signedIn, [])
})

for (const succeeds of [true, false]) {
  test(`unmount aborts and fences delayed sign-in reconciliation (${succeeds})`, async t => {
    const s = setup(t)
    const { running } = await s.start()
    s.post.resolve(response({ detail: 'Ambiguous completion' }, 503))
    await flush()
    assert.equal(s.calls.length, 3)
    s.view.unmount()
    s.reconciliation.resolve(succeeds ? response(identity) : response({ detail: 'Unavailable' }, 503))
    await running
    assert.equal(s.calls[2].options.signal?.aborted, true)
    assert.deepEqual(s.signedIn, [])
  })
}

for (const reconcile of [false, true]) {
  test(`active sign-in still accepts ${reconcile ? 'authoritative reconciliation' : 'successful completion'}`, async t => {
    const s = setup(t)
    const { running } = await s.start()
    s.post.resolve(reconcile ? response({ detail: 'Ambiguous completion' }, 503) : response(identity))
    if (reconcile) {
      await flush()
      s.reconciliation.resolve(response(identity))
    }
    await running
    assert.deepEqual(s.signedIn, [identity])
  })
}
