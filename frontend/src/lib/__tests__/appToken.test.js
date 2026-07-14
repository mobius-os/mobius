/**
 * Unit tests for lib/appToken.js — the in-shell mini-app token selection.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/appToken.test.js
 *
 * Pure functions, no React/DOM — plain node:test. The headline test
 * (`reproduce-android-online-flap`) encodes the exact on-device failure that
 * was invisible to every desktop browser harness: an oscillating `online`
 * signal must NOT revoke a token we already resolved, because that unmounted
 * the live iframe and span forever.
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  liveAppToken,
  latchedAppToken,
  resolveLatchedToken,
  appTokenRefreshInterval,
  _resetLatchStore,
} from '../appToken.js'

beforeEach(() => { _resetLatchStore() })

const CACHED = 'cached-app-scoped-tok'
const APP = 'app-scoped-tok'

function tokenWithExpiry(exp) {
  const payload = Buffer.from(JSON.stringify({ exp })).toString('base64url')
  return `header.${payload}.signature`
}

test('app token refresh follows JWT expiry with a five-minute safety window', () => {
  const now = Date.UTC(2026, 6, 13, 12, 0, 0)
  const expires = now + 8 * 60 * 60_000
  assert.equal(appTokenRefreshInterval(tokenWithExpiry(expires / 1000), now), 7 * 60 * 60_000 + 55 * 60_000)
})

test('app token refresh is bounded for nearly-expired and malformed tokens', () => {
  const now = Date.UTC(2026, 6, 13, 12, 0, 0)
  assert.equal(appTokenRefreshInterval(tokenWithExpiry((now + 60_000) / 1000), now), 30_000)
  assert.equal(appTokenRefreshInterval('not-a-jwt', now), 5 * 60_000)
})

test('liveAppToken: app-scoped token always wins', () => {
  assert.equal(liveAppToken(APP, true, CACHED), APP)
  assert.equal(liveAppToken(APP, false, CACHED), APP)
})

test('liveAppToken: online with no fresh token waits instead of using the cache', () => {
  assert.equal(liveAppToken(undefined, true, CACHED), undefined)
})

test('liveAppToken: offline uses the cached app-scoped token', () => {
  assert.equal(liveAppToken(undefined, false, CACHED), CACHED)
})

test('liveAppToken: offline with no cached app token → undefined', () => {
  assert.equal(liveAppToken(undefined, false, null), undefined)
})

test('latchedAppToken: holds the latch when the live token blips to undefined', () => {
  // The latch was set offline; a transient online=true makes liveToken
  // undefined, but the latch must keep the resolved token.
  assert.equal(latchedAppToken(undefined, CACHED), CACHED)
})

test('latchedAppToken: a fresh app-scoped token supersedes the latch', () => {
  assert.equal(latchedAppToken(APP, CACHED), APP)
})

test('latchedAppToken: empty when nothing resolved yet', () => {
  assert.equal(latchedAppToken(undefined, undefined), undefined)
})

// The regression test: replay the on-device flap sequence and assert the
// token (hence the mounted iframe) never drops out once resolved.
test('reproduce-android-online-flap: token stays stable across online oscillation', () => {
  // Simulate the AppCanvas latch across a render sequence. `latched` is the
  // useRef the component holds; we update it exactly as the component does:
  // `if (liveToken) latched = liveToken`.
  let latched = undefined
  const step = (appToken, online, cachedToken) => {
    const live = liveAppToken(appToken, online, cachedToken)
    if (live) latched = live
    return latchedAppToken(appToken, latched)
  }

  // 1. Online boot, app-scoped token resolves → app mounts with APP token.
  assert.equal(step(APP, true, CACHED), APP, 'online boot uses app token')

  // 2. Go offline (airplane). No app-token offline; reachability=false →
  //    persisted app token. App stays mounted.
  assert.equal(step(undefined, false, CACHED), CACHED, 'offline uses the persisted app token')

  // 3. THE FLAP: navigator.onLine reports stale `true` → online=true for a
  //    render, no app-token. OLD code returned undefined here → iframe
  //    unmount → spinner. With the latch, the token MUST hold.
  assert.equal(step(undefined, true, CACHED), CACHED, 'online blip must NOT drop the token')

  // 4. Flip back to offline. Still stable.
  assert.equal(step(undefined, false, CACHED), CACHED, 'still stable after flap')

  // 5. Several more oscillations — token never drops to undefined.
  for (let i = 0; i < 6; i++) {
    const online = i % 2 === 0
    assert.ok(step(undefined, online, CACHED), `flap iter ${i} keeps a token`)
  }
})

// resolveLatchedToken is the real component path: it must survive a REMOUNT
// (module-scoped store, not a useRef) and reset synchronously on app switch.
test('resolveLatchedToken: holds token across a simulated AppCanvas remount', () => {
  // First mount: online boot resolves app token.
  assert.equal(resolveLatchedToken(22, 0, APP, APP), APP)
  // Go offline: cached app token is latched.
  assert.equal(resolveLatchedToken(22, 0, CACHED, undefined), CACHED)
  // *** REMOUNT *** — in the real component every useRef would reset here.
  // We simulate it by simply calling again with the flap's bad live value
  // (online blip → liveToken undefined). The module store must still hold.
  assert.equal(resolveLatchedToken(22, 0, undefined, undefined), CACHED,
    'token must survive a remount during the online flap')
  // Several flap iterations across "remounts" — never drops.
  for (let i = 0; i < 5; i++) {
    assert.ok(resolveLatchedToken(22, 0, undefined, undefined), `remount flap ${i}`)
  }
})

test('resolveLatchedToken: a different app does NOT inherit the previous latch', () => {
  assert.equal(resolveLatchedToken(22, 0, CACHED, undefined), CACHED)
  // Switch to app 99 while offline with no live token — must NOT get app 22's.
  assert.equal(resolveLatchedToken(99, 0, undefined, undefined), undefined,
    'a switched app must start with no latched token')
})

test('resolveLatchedToken: a version bump resets the latch for the same app', () => {
  assert.equal(resolveLatchedToken(22, 0, CACHED, undefined), CACHED)
  // version 1 of the same app is a real teardown → fresh.
  assert.equal(resolveLatchedToken(22, 1, undefined, undefined), undefined,
    'a version bump must not reuse the old version latch')
})

// The iframe LRU mounts up to 4 AppCanvas siblings concurrently; each renders
// and calls resolveLatchedToken. One sibling's render must NOT evict another
// sibling's latch (the bug if we deleted "all other keys" — caught by Codex).
test('resolveLatchedToken: concurrent sibling apps keep their own latches', () => {
  const A = 'tok-A', B = 'tok-B'
  assert.equal(resolveLatchedToken(10, 0, A, undefined), A)
  assert.equal(resolveLatchedToken(20, 0, B, undefined), B)
  // Re-render app 10 during a flap (no live token) — still A, and 20 untouched.
  assert.equal(resolveLatchedToken(10, 0, undefined, undefined), A, 'app 10 keeps its latch')
  assert.equal(resolveLatchedToken(20, 0, undefined, undefined), B, 'app 20 keeps its latch')
  assert.equal(resolveLatchedToken(10, 0, undefined, undefined), A)
  assert.equal(resolveLatchedToken(20, 0, undefined, undefined), B)
})

test('clearLatchedTokens drops everything (logout)', () => {
  assert.equal(resolveLatchedToken(10, 0, 'x', undefined), 'x')
  _resetLatchStore()
  assert.equal(resolveLatchedToken(10, 0, undefined, undefined), undefined,
    'after logout the latch must be empty')
})

// Guard the security intent: a GENUINE online session that never went offline
// must wait for a fresh token instead of silently trusting persisted auth.
test('genuine online session waits for a fresh app token', () => {
  let latched = undefined
  const step = (appToken, online, cachedToken) => {
    const live = liveAppToken(appToken, online, cachedToken)
    if (live) latched = live
    return latchedAppToken(appToken, latched)
  }
  // Online the whole time, app-scoped token slow to resolve.
  assert.equal(step(undefined, true, CACHED), undefined)
  assert.equal(step(undefined, true, CACHED), undefined)
  // Fresh app token finally resolves and supersedes any persisted token.
  assert.equal(step(APP, true, CACHED), APP)
})
