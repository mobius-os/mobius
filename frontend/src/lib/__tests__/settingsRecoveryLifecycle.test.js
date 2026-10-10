import { test } from 'node:test'
import assert from 'node:assert/strict'
import { RecoverySection } from '../../components/SettingsView/identity/IdentityAccount.jsx'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

const flush = () => new Promise(resolve => setImmediate(resolve))
const response = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } })
function deferred() {
  let resolve
  const promise = new Promise(r => { resolve = r })
  return { promise, resolve }
}
function setup(t, blocked = false) {
  const oldWindow = globalThis.window
  let closes = 0
  const navigations = []
  const opens = []
  const timers = new Map()
  let nextTimer = 0
  const calls = []
  const popup = { closed: false, close() { closes++ }, location: { replace(url) { navigations.push(url) } } }
  globalThis.window = { open(...args) { opens.push(args); return blocked ? null : popup } }
  t.after(() => { if (oldWindow === undefined) delete globalThis.window; else globalThis.window = oldWindow })
  t.mock.method(globalThis, 'setTimeout', fn => { timers.set(++nextTimer, fn); return nextTimer })
  t.mock.method(globalThis, 'clearTimeout', id => timers.delete(id))
  const post = deferred()
  const status = deferred()
  t.mock.method(globalThis, 'fetch', (url, options) => {
    calls.push({ url, options })
    return calls.length === 1 ? post.promise : status.promise
  })
  const view = renderHook(props => RecoverySection(props), { token: 'mock-token', instance: { id: 'dep-example' } })
  t.after(() => view.unmount())
  const start = () => view.result.current.props.children[0].props.onClick()
  return { view, start, post, status, calls, opens, timers, navigations, popup, get closes() { return closes } }
}

test('unmount aborts and fences a delayed recovery POST before polling', async t => {
  const s = setup(t)
  const running = s.start()
  s.view.unmount()
  s.post.resolve(response({}))
  await running
  await flush()
  assert.equal(s.calls.length, 1)
  assert.equal(s.calls[0].options.signal?.aborted, true)
  assert.equal(s.timers.size, 0)
  assert.equal(s.navigations.length, 0)
  assert.equal(s.closes, 1)
})

for (const state of ['starting', 'ready']) {
  test(`unmount fences delayed ${state} recovery status from rearming or opening`, async t => {
    const s = setup(t)
    const running = s.start()
    s.post.resolve(response({}))
    await flush()
    assert.equal(s.calls.length, 2)
    const reservedWindows = s.opens.length
    s.view.unmount()
    s.status.resolve(response({ state, open_url: 'https://recovery.example' }))
    await running
    await flush()
    assert.equal(s.timers.size, 0)
    assert.equal(s.opens.length, reservedWindows)
    assert.equal(s.navigations.length, 0)
    assert.equal(s.calls[1].options.signal?.aborted, true)
  })
}

test('a blocked reservation never starts a recovery worker', async t => {
  const s = setup(t, true)
  s.start()
  assert.equal(s.calls.length, 0)
  assert.match(JSON.stringify(s.view.result.current), /blocked/)
})

test('recovery reserves on the click and navigates only that popup when ready', async t => {
  const s = setup(t)
  const running = s.start()
  assert.equal(s.opens[0][0], 'about:blank')
  // The busy guard also rejects a second click before the async request returns.
  await s.start()
  assert.equal(s.calls.length, 1)
  s.post.resolve(response({}))
  await flush()
  s.status.resolve(response({ state: 'ready', open_url: 'https://recovery.example' }))
  await running
  await flush()
  assert.deepEqual(s.navigations, ['https://recovery.example'])
  assert.equal(s.opens.length, 1)
  assert.equal(s.closes, 0)
  assert.equal(s.timers.size, 0)
})

for (const props of [
  { token: 'replacement-token', instance: { id: 'dep-example' } },
  { token: 'mock-token', instance: { id: 'other-deployment' } },
]) {
  test(`changing ${props.token === 'mock-token' ? 'deployment' : 'token'} retires the old recovery session`, async t => {
    const s = setup(t)
    const running = s.start()
    s.view.rerender(props)
    s.post.resolve(response({}))
    await running
    await flush()
    assert.equal(s.calls.length, 1)
    assert.equal(s.calls[0].options.signal?.aborted, true)
    assert.equal(s.closes, 1)
    assert.equal(s.timers.size, 0)
  })
}

test('starting recovery arms one timer and unmount clears it and its reservation', async t => {
  const s = setup(t)
  const running = s.start()
  s.post.resolve(response({}))
  await flush()
  s.status.resolve(response({ state: 'starting' }))
  await running
  await flush()
  assert.equal(s.timers.size, 1)
  s.view.unmount()
  assert.equal(s.timers.size, 0)
  assert.equal(s.closes, 1)
})

test('closed recovery reservation never navigates or reopens on a delayed ready result', async t => {
  const s = setup(t)
  const running = s.start()
  s.post.resolve(response({}))
  await flush()
  s.popup.closed = true
  s.status.resolve(response({ state: 'ready', open_url: 'https://recovery.example' }))
  await running
  await flush()
  assert.equal(s.opens.length, 1)
  assert.equal(s.navigations.length, 0)
  assert.equal(s.timers.size, 0)
  assert.match(JSON.stringify(s.view.result.current), /window was closed/)
})

test('a failed recovery POST closes the blank reservation and exposes the service error', async t => {
  const s = setup(t)
  const running = s.start()
  s.post.resolve(new Response(JSON.stringify({ detail: 'Recovery unavailable' }), { status: 503 }))
  await running
  assert.equal(s.closes, 1)
  assert.equal(s.calls.length, 1)
  assert.equal(s.timers.size, 0)
  assert.match(JSON.stringify(s.view.result.current), /Recovery unavailable/)
})
