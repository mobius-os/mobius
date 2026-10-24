/**
 * Execute the production frame loader/listener and AppCanvas message adapter.
 * Only the module-import/browser-render boundaries are doubled: no blob module
 * evaluation or React mounting is claimed here.
 *
 * cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *   --test src/lib/__tests__/tokenExpiredProtocol.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse } from 'acorn'
import { QueryClient } from '@tanstack/react-query'
import { appQueries } from '../../hooks/queries.js'
import { resolveLatchedToken } from '../appToken.js'
import { accountLinkCompletion } from '../accountLinkBroker.js'
import { appHostRequest } from '../appHostRequest.js'
import {
  appMediaSessionEvent, applyVirtualStorageMutation, attributedFrameVersion,
} from '../../components/AppCanvas/appFrameProtocol.js'

const frameSource = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url), 'utf8',
)
const canvasSource = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url), 'utf8',
)
const frameScript = [...frameSource.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(script => script.includes('function renegotiateExpiredModuleToken('))
assert.ok(frameScript, 'the frame runtime script must exist')
const frameAst = parse(frameScript, { ecmaVersion: 'latest', sourceType: 'module' })
function frameFunction(name) {
  const node = frameAst.body.find(node => node.type === 'FunctionDeclaration' && node.id.name === name)
  assert.ok(node, `production frame function ${name} must exist`)
  return frameScript.slice(node.start, node.end)
}
const frameListener = frameAst.body.find(node => {
  if (node.type !== 'ExpressionStatement') return false
  const call = node.expression
  return call.type === 'CallExpression'
    && call.callee.type === 'MemberExpression'
    && call.callee.object.name === 'window'
    && call.callee.property.name === 'addEventListener'
    && call.arguments[0]?.value === 'message'
    && frameScript.slice(node.start, node.end).includes('moebius:frame-init')
})
assert.ok(frameListener, 'the actual frame-init listener must be registered')

function frameHarness(failures = []) {
  const posts = []
  const errors = []
  const imports = []
  const renders = []
  let listener
  let api
  const window = {
    location: { origin: 'https://mobius.test' },
    parent: { postMessage(message, origin) {
      // Capture the latch AT post time, not just the eventual final state.
      posts.push({ message, origin, initialized: api.initialized() })
    } },
    addEventListener(type, callback) {
      assert.equal(type, 'message')
      listener = callback
    },
  }
  api = runInNewContext(`
    let initialized = true, currentToken = 'expired-token', _reportToken = null;
    let currentCapabilityContract = null, currentNavLocation = null;
    let waitForNavigationReady = false;
    let tokenRefreshResolve = null, tokenRefreshPromise = null;
    const _FRAME_APP_ID = 'protocol-app';
    ${['renegotiateExpiredModuleToken', 'isBlobModuleLoadFailure',
       'tokenAppInstanceId', 'tokenExpiresSoon', 'requestFreshToken', 'runtimeToken',
       'renderMountedComponent', 'acceptToken', 'loadModule'].map(frameFunction).join('\n')}
    ${frameScript.slice(frameListener.start, frameListener.end)}
    ({ renegotiateExpiredModuleToken, initialized: () => initialized,
       token: () => currentToken, reportToken: () => _reportToken, runtimeToken })
  `, {
    window,
    atob,
    // React render boundary: observe props from the real render helper.
    mountedRoot: { render(element) { renders.push(element) } },
    mountedComponent: 'App', MountSignal: 'MountSignal', Fragment: 'Fragment',
    createElement(type, props, ...children) { return { type, props, children } },
    applyTheme() {},
    async importBrokeredModule(retry) {
      imports.push(retry)
      throw failures.shift() || Object.assign(new Error('terminal fixture failure'), { code: 'http' })
    },
    showErr(...args) { errors.push(args) },
    setTimeout(callback) { callback() }, // Advance only the loader's bounded retry delay.
  })
  return { api, window, posts, imports, errors, renders,
    message(data, { source = window.parent, origin = window.location.origin } = {}) {
      listener({ data, source, origin })
    },
  }
}
const flushLoader = () => new Promise(resolve => setImmediate(resolve))
const init = token => ({ type: 'moebius:frame-init', token })
const failure = code => Object.assign(new Error(code), { code })

test('actual token-expired helper resets the init latch before posting and returns true', () => {
  const h = frameHarness()
  assert.equal(h.api.renegotiateExpiredModuleToken(failure('token-expired')), true)
  assert.equal(h.api.initialized(), false)
  assert.equal(h.posts.length, 1)
  assert.deepEqual({ ...h.posts[0].message }, { type: 'moebius:token-expired', appId: 'protocol-app' })
  assert.equal(h.posts[0].origin, h.window.location.origin)
  assert.equal(h.posts[0].initialized, false)
  assert.deepEqual(h.errors, [])
})

for (const code of [undefined, 'network', 'http', 'module-load-failed']) {
  test(`actual helper does not rotate credentials for ${code || 'absent error'}`, () => {
    const h = frameHarness()
    assert.equal(h.api.renegotiateExpiredModuleToken(code ? failure(code) : null), false)
    assert.equal(h.api.initialized(), true)
    assert.deepEqual(h.posts, [])
  })
}

for (const retry of [false, true]) {
  test(`registered frame-init loader recovers typed expiry (retry=${retry}) without an error panel`, async () => {
    const failures = retry ? [failure('network'), failure('token-expired')] : [failure('token-expired')]
    const h = frameHarness(failures)
    h.api.renegotiateExpiredModuleToken(failure('token-expired'))
    h.posts.length = 0
    h.message(init('fresh-token'), { source: {} })
    h.message(init('fresh-token'), { origin: 'https://attacker.test' })
    assert.equal(h.api.token(), 'expired-token')
    assert.equal(h.api.initialized(), false)
    assert.deepEqual(h.imports, [])

    h.message(init('fresh-token'))
    await flushLoader()
    assert.deepEqual(h.imports, retry ? [0, 1] : [0])
    assert.equal(h.posts.length, 1, 'the real import catch must request fresh credentials')
    assert.equal(h.posts[0].message.type, 'moebius:token-expired')
    assert.equal(h.posts[0].initialized, false)
    assert.equal(h.api.token(), 'fresh-token')
    assert.equal(h.api.reportToken(), 'fresh-token')
    assert.deepEqual(h.errors, [], 'expiry must not fall through to showErr')

    // Reset latch must let the next genuine parent init attempt the load again.
    h.message(init('refreshed-again'))
    await flushLoader()
    assert.equal(h.api.token(), 'refreshed-again')
    assert.equal(h.imports.length, (retry ? 2 : 1) + 1)
  })
}

test('network and HTTP load failures reach the error surface without an auth-rotation loop', async () => {
  for (const code of ['network', 'http', 'module-load-failed']) {
    const h = frameHarness([failure(code), failure(code)])
    h.api.renegotiateExpiredModuleToken(failure('token-expired'))
    h.posts.length = 0
    h.message(init('still-valid-token'))
    await flushLoader()
    assert.deepEqual(h.imports, code === 'network' ? [0, 1] : [0])
    assert.deepEqual(h.posts, [])
    assert.equal(h.api.initialized(), true)
    assert.equal(h.errors.length, 1)
  }
})

test('an already-initialized frame updates runtime and component credentials without reimporting', async () => {
  const h = frameHarness()
  const token = `header.${Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 3600 })).toString('base64url')}.signature`
  h.message(init(token))
  assert.equal(h.api.token(), token)
  assert.equal(h.api.reportToken(), token)
  assert.equal(await h.api.runtimeToken(), token)
  assert.equal(h.renders.length, 1)
  assert.equal(h.renders[0].children[1].props.token, token)
  assert.deepEqual(h.imports, [])
})

// Extract the actual adapter THROUGH its registration, not a reimplementation
// of the token-expired branch. Unrelated capability/navigation handlers are not
// exercised. The real attribution, virtual-storage gate, and query methods run.
function canvasHarness(queryClient, appId) {
  const start = canvasSource.indexOf('    function onMessage(e) {')
  const registration = "window.addEventListener('message', onMessage)"
  const end = canvasSource.indexOf(registration, start)
  assert.ok(start >= 0 && end > start, 'AppCanvas must register its actual message handler')
  let listener
  const source = {}
  runInNewContext(canvasSource.slice(start, end + registration.length), {
    appId, appQueries, queryClient,
    window: { location: { origin: 'https://mobius.test' }, addEventListener(type, callback) {
      assert.equal(type, 'message')
      listener = callback
    } },
    framesRef: { current: new Map([[7, { contentWindow: source }]]) },
    liveVersionRef: { current: 7 },
    accountLinkRef: { current: null },
    accountLinkCompletion, attributedFrameVersion, applyVirtualStorageMutation,
    appMediaSessionEvent, appHostRequest,
    capabilityHostRef: { current: { handle: () => false } },
  })
  assert.equal(typeof listener, 'function')
  return { source, message(data, overrides = {}) {
    listener({ source, origin: 'null', data, ...overrides })
  } }
}

test('registered AppCanvas expiry handler invalidates only the real app-token query and preserves offline latches', () => {
  const appId = 'expiry-protocol-app'
  const sibling = 'expiry-protocol-sibling'
  const client = new QueryClient()
  try {
    const key = appQueries.token.key(appId)
    const siblingKey = appQueries.token.key(sibling)
    client.setQueryData(key, 'expired')
    client.setQueryData(siblingKey, 'sibling-token')
    client.setQueryData(['chats'], ['chat'])
    resolveLatchedToken(appId, 7, 'expired', 'expired')
    resolveLatchedToken(sibling, 7, 'sibling-token', 'sibling-token')
    const h = canvasHarness(client, appId)
    const expired = { type: 'moebius:token-expired', appId }
    h.message(expired, { source: {} })
    h.message(expired, { origin: 'https://attacker.test' })
    h.message({ ...expired, appId: sibling })
    assert.equal(client.getQueryState(key).isInvalidated, false)

    h.message(expired)
    assert.equal(client.getQueryState(key).isInvalidated, true)
    assert.equal(client.getQueryState(siblingKey).isInvalidated, false)
    assert.equal(client.getQueryState(['chats']).isInvalidated, false)
    assert.equal(resolveLatchedToken(appId, 7, undefined, undefined), 'expired')
    assert.equal(resolveLatchedToken(sibling, 7, undefined, undefined), 'sibling-token')

    // The normal fresh-token selection replaces only this app's latch.
    assert.equal(resolveLatchedToken(appId, 7, 'fresh', 'fresh'), 'fresh')
    assert.equal(resolveLatchedToken(sibling, 7, undefined, undefined), 'sibling-token')
    assert.match(canvasSource, /resolveLatchedToken\(appId, version, liveToken, appToken\)/)
  } finally {
    client.clear()
  }
})

test('app error reporting remains wired to the refreshable token broker', () => {
  assert.match(frameSource, /runtimeToken\(\{ forceRefresh: true \}\)/)
  assert.doesNotMatch(frameSource, /Authorization': 'Bearer ' \+ _reportToken/)
})
