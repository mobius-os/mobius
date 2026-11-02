import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  immersiveLifecycleValue,
  immersiveReducer,
  isImmersiveActive,
} from '../immersive.js'

test('request value:true grants the immersive slot to the app', () => {
  assert.deepEqual(
    immersiveReducer(null, { type: 'request', appId: 7, value: true }),
    { appId: 7, mode: 'full' },
  )
})

test('request value:false from the holder releases the slot', () => {
  assert.equal(immersiveReducer(
    { appId: 7, mode: 'full' },
    { type: 'request', appId: 7, value: false },
  ), null)
})

test('request value:false from a non-holder leaves the slot alone', () => {
  // A hidden cached iframe being evicted (or posting its own cleanup)
  // must not strip another app's immersive request.
  const request = { appId: 7, mode: 'bar' }
  assert.equal(
    immersiveReducer(request, { type: 'request', appId: 3, value: false }),
    request,
  )
})

test('a later request replaces owner and mode atomically', () => {
  assert.deepEqual(immersiveReducer(
    { appId: 7, mode: 'full' },
    { type: 'request', appId: 3, value: true, mode: 'bar' },
  ), { appId: 3, mode: 'bar' })
})

test('release tolerates numeric/string id mismatch', () => {
  // Shell passes numeric ids from /api/apps; some paths stringify.
  assert.equal(immersiveReducer(
    { appId: 7, mode: 'full' },
    { type: 'request', appId: '7', value: false },
  ), null)
})

test('exit (the shell button) always clears, whoever holds it', () => {
  assert.equal(immersiveReducer({ appId: 7, mode: 'bar' }, { type: 'exit' }), null)
  assert.equal(immersiveReducer(null, { type: 'exit' }), null)
})

test('unknown actions are a no-op', () => {
  const request = { appId: 7, mode: 'full' }
  assert.equal(immersiveReducer(request, { type: 'bogus' }), request)
})

test('immersive applies only on the canvas view with the holder active', () => {
  const request = { appId: 7, mode: 'full' }
  assert.equal(isImmersiveActive(request, 'canvas', 7), true)
  // Same request, but the user is looking elsewhere — chrome stays.
  assert.equal(isImmersiveActive(request, 'chat', null), false)
  assert.equal(isImmersiveActive(request, 'settings', null), false)
  // Another app is active — the holder's request is dormant, not applied.
  assert.equal(isImmersiveActive(request, 'canvas', 3), false)
})

test('immersive application tolerates numeric/string id mismatch', () => {
  assert.equal(isImmersiveActive({ appId: '7', mode: 'full' }, 'canvas', 7), true)
})

test('no holder means no immersive regardless of view', () => {
  assert.equal(isImmersiveActive(null, 'canvas', 7), false)
})

test('leaving an app releases immersive intent', () => {
  assert.equal(immersiveLifecycleValue(
    { appId: 7, liveVersion: 'a', active: true },
    { appId: 7, liveVersion: 'a', active: false },
    true,
  ), false)
})

test('returning to a cached app never resurrects its old immersive request', () => {
  assert.equal(immersiveLifecycleValue(
    { appId: 7, liveVersion: 'a', active: false },
    { appId: 7, liveVersion: 'a', active: true },
    true,
  ), null)
})

test('a request made on this return, before the pane activated, is honoured', () => {
  // The shell shows the frame before it marks the pane active; an app that
  // asks as soon as it reappears must not lose that fresh request.
  const previous = { appId: 7, liveVersion: 'a', active: false }
  const current = { appId: 7, liveVersion: 'a', active: true }
  assert.equal(immersiveLifecycleValue(previous, current, true, true), true)
  assert.equal(immersiveLifecycleValue(previous, current, false, true), null)
  // Leaving still releases even if a request was recorded.
  assert.equal(immersiveLifecycleValue(current, previous, true, true), false)
})

test('a live frame promotion replays only the promoted frame intent', () => {
  const previous = { appId: 7, liveVersion: 'a', active: true }
  const current = { appId: 7, liveVersion: 'b', active: true }
  assert.equal(immersiveLifecycleValue(previous, current, true), true)
  assert.equal(immersiveLifecycleValue(previous, current, false), false)
})

test('initial mount is driven by the frame message, not a stale replay', () => {
  assert.equal(immersiveLifecycleValue(
    null,
    { appId: 7, liveVersion: 'a', active: true },
    true,
  ), null)
})
