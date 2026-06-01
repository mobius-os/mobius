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
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { liveAppToken, latchedAppToken } from '../appToken.js'

const OWNER = 'owner-jwt'
const APP = 'app-scoped-tok'

test('liveAppToken: app-scoped token always wins', () => {
  assert.equal(liveAppToken(APP, true, OWNER), APP)
  assert.equal(liveAppToken(APP, false, OWNER), APP)
})

test('liveAppToken: online with no app-token → undefined (never substitutes owner JWT)', () => {
  assert.equal(liveAppToken(undefined, true, OWNER), undefined)
})

test('liveAppToken: offline with no app-token → owner JWT (so cached app still boots)', () => {
  assert.equal(liveAppToken(undefined, false, OWNER), OWNER)
})

test('liveAppToken: offline with no owner JWT → undefined (nothing to use)', () => {
  assert.equal(liveAppToken(undefined, false, null), undefined)
})

test('latchedAppToken: holds the latch when the live token blips to undefined', () => {
  // The latch was set offline; a transient online=true makes liveToken
  // undefined, but the latch must keep the resolved token.
  assert.equal(latchedAppToken(undefined, OWNER), OWNER)
})

test('latchedAppToken: a fresh app-scoped token supersedes the latch', () => {
  assert.equal(latchedAppToken(APP, OWNER), APP)
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
  const step = (appToken, online, ownerToken) => {
    const live = liveAppToken(appToken, online, ownerToken)
    if (live) latched = live
    return latchedAppToken(appToken, latched)
  }

  // 1. Online boot, app-scoped token resolves → app mounts with APP token.
  assert.equal(step(APP, true, OWNER), APP, 'online boot uses app token')

  // 2. Go offline (airplane). No app-token offline; reachability=false →
  //    owner JWT. App stays mounted.
  assert.equal(step(undefined, false, OWNER), OWNER, 'offline falls back to owner JWT')

  // 3. THE FLAP: navigator.onLine reports stale `true` → online=true for a
  //    render, no app-token. OLD code returned undefined here → iframe
  //    unmount → spinner. With the latch, the token MUST hold.
  assert.equal(step(undefined, true, OWNER), OWNER, 'online blip must NOT drop the token')

  // 4. Flip back to offline. Still stable.
  assert.equal(step(undefined, false, OWNER), OWNER, 'still stable after flap')

  // 5. Several more oscillations — token never drops to undefined.
  for (let i = 0; i < 6; i++) {
    const online = i % 2 === 0
    assert.ok(step(undefined, online, OWNER), `flap iter ${i} keeps a token`)
  }
})

// Guard the security intent: a GENUINE online session that never went offline
// must never end up holding the owner JWT (the latch can only hold what the
// live selection produced, and online-with-no-app-token produces undefined).
test('genuine online session never latches the owner JWT', () => {
  let latched = undefined
  const step = (appToken, online, ownerToken) => {
    const live = liveAppToken(appToken, online, ownerToken)
    if (live) latched = live
    return latchedAppToken(appToken, latched)
  }
  // Online the whole time, app-scoped token slow to resolve.
  assert.equal(step(undefined, true, OWNER), undefined)
  assert.equal(step(undefined, true, OWNER), undefined)
  // App token finally resolves → used; owner JWT was never latched.
  assert.equal(step(APP, true, OWNER), APP)
})
