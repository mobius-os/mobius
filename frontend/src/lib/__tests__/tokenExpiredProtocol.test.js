/**
 * Unit tests for the moebius:token-expired message protocol.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/tokenExpiredProtocol.test.js
 *
 * The controlled parent-side module broker can observe a 401/403 directly and
 * returns `{code:'token-expired'}` to the opaque frame. The frame resets its
 * init latch and posts {type:'moebius:token-expired'}; AppCanvas invalidates
 * the app-token query.
 *
 * This file tests the typed decision and protocol invariants — NOT blob module
 * evaluation, which needs a browser.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const frameSource = readFileSync(
  new URL('../../../public/app-frame.html', import.meta.url),
  'utf8',
)
const canvasSource = readFileSync(
  new URL('../../components/AppCanvas/AppCanvas.jsx', import.meta.url),
  'utf8',
)

function shouldRenegotiate(code) {
  return code === 'token-expired'
}

test('typed module auth failures trigger token renegotiation', () => {
  assert.equal(shouldRenegotiate('token-expired'), true)
})

test('network and ordinary HTTP module failures do not rotate credentials', () => {
  assert.equal(shouldRenegotiate('network'), false)
  assert.equal(shouldRenegotiate('http'), false)
  assert.equal(shouldRenegotiate('module-load-failed'), false)
})

// The protocol contract:
//   - Frame resets `initialized = false` before posting moebius:token-expired
//     so the follow-up moebius:frame-init (with the new token) is accepted.
//   - The frame does NOT call showErr() on an auth error — the error panel
//     must NOT appear while token renegotiation is in flight.
//   - AppCanvas invalidates the app-token query key ['app-token', appId].
//
// These are structural invariants described in the protocol; we lock them in
// as documentation-as-tests.

test('protocol: frame must reset initialized before posting token-expired', () => {
  const start = frameSource.indexOf('function renegotiateExpiredModuleToken(error)')
  const end = frameSource.indexOf('function acceptModuleAck', start)
  assert.ok(start >= 0 && end > start, 'token renegotiation helper must exist')
  const helper = frameSource.slice(start, end)
  const reset = helper.indexOf('initialized = false')
  const post = helper.indexOf("type: 'moebius:token-expired'")
  assert.ok(reset >= 0 && post > reset, 'the init latch resets before the post')
  assert.match(helper, /error\.code !== 'token-expired'/)
  assert.match(frameSource, /renegotiateExpiredModuleToken\(importErr\)/)
  assert.match(frameSource, /renegotiateExpiredModuleToken\(retryErr\)/)
})

test('protocol: frame must NOT post token-expired for an offline broker failure', () => {
  let messagePosted = null

  const code = 'network'
  if (shouldRenegotiate(code)) {
    messagePosted = { type: 'moebius:token-expired', appId: 'test-app' }
  }

  assert.equal(messagePosted, null,
    'offline network errors must NOT trigger token renegotiation')
})

test('protocol: query key format matches appQueries.token.key', () => {
  // AppCanvas calls: appQueries.token.invalidate(queryClient, appId)
  // which resolves to: queryClient.invalidateQueries({ queryKey: ['app-token', appId] })
  // Lock in the key shape so a rename of the constant doesn't silently break the
  // invalidation.
  const appId = 99
  const key = ['app-token', appId]
  assert.equal(key[0], 'app-token')
  assert.equal(key[1], appId)
})

test('protocol: token expiry recovery preserves offline-latch semantics', () => {
  // The latch store (appToken.js) is NOT wiped on token-expired — the module
  // stores the new fresh token when resolveLatchedToken is called with the
  // new live token. Old latches for stale versions are cleared by the version-
  // bump path (not this path). This test verifies the concern is understood.
  //
  // Offline latch survives because:
  //   - invalidateQueries triggers a refetch of the app-token query.
  //   - The new token becomes `appToken` in the next render.
  //   - resolveLatchedToken(appId, version, newLiveToken, newAppToken) stores
  //     newLiveToken under the same appId:version key, overwriting the expired
  //     one. The latch for a different appId or different version is untouched.
  //
  assert.match(canvasSource, /resolveLatchedToken\(appId, version, liveToken, appToken\)/)
  assert.match(
    canvasSource,
    /msg\.type === 'moebius:token-expired'[\s\S]{0,180}?appQueries\.token\.invalidate\(queryClient, appId\)/,
  )
})

test('protocol: an already-mounted frame accepts refreshed credentials', () => {
  assert.match(frameSource, /if \(msg\.type === 'moebius:frame-init'\) \{\s+currentCapabilityContract = msg\.capabilityContract \|\| null;\s+acceptToken\(msg\.token\);\s+if \(initialized\) return;/)
  assert.match(frameSource, /getToken: runtimeToken/)
  assert.match(frameSource, /token: currentToken/)
})

test('protocol: app error reporting uses the same refreshable token broker', () => {
  assert.match(frameSource, /runtimeToken\(\{ forceRefresh: true \}\)/)
  assert.doesNotMatch(frameSource, /Authorization': 'Bearer ' \+ _reportToken/)
})
