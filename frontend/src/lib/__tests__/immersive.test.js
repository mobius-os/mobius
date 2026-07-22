import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  immersiveLifecycleValue,
  immersiveReducer,
  isImmersiveActive,
} from '../immersive.js'

test('request value:true grants the immersive slot to the app', () => {
  assert.equal(immersiveReducer(null, { type: 'request', appId: 7, value: true }), 7)
})

test('request value:false from the holder releases the slot', () => {
  assert.equal(immersiveReducer(7, { type: 'request', appId: 7, value: false }), null)
})

test('request value:false from a non-holder leaves the slot alone', () => {
  // A hidden cached iframe being evicted (or posting its own cleanup)
  // must not strip another app's immersive request.
  assert.equal(immersiveReducer(7, { type: 'request', appId: 3, value: false }), 7)
})

test('a later request from another app wins the slot', () => {
  assert.equal(immersiveReducer(7, { type: 'request', appId: 3, value: true }), 3)
})

test('release tolerates numeric/string id mismatch', () => {
  // Shell passes numeric ids from /api/apps; some paths stringify.
  assert.equal(immersiveReducer(7, { type: 'request', appId: '7', value: false }), null)
})

test('exit (the shell button) always clears, whoever holds it', () => {
  assert.equal(immersiveReducer(7, { type: 'exit' }), null)
  assert.equal(immersiveReducer(null, { type: 'exit' }), null)
})

test('unknown actions are a no-op', () => {
  assert.equal(immersiveReducer(7, { type: 'bogus' }), 7)
})

test('immersive applies only on the canvas view with the holder active', () => {
  assert.equal(isImmersiveActive(7, 'canvas', 7), true)
  // Same request, but the user is looking elsewhere — chrome stays.
  assert.equal(isImmersiveActive(7, 'chat', null), false)
  assert.equal(isImmersiveActive(7, 'settings', null), false)
  // Another app is active — the holder's request is dormant, not applied.
  assert.equal(isImmersiveActive(7, 'canvas', 3), false)
})

test('immersive application tolerates numeric/string id mismatch', () => {
  assert.equal(isImmersiveActive('7', 'canvas', 7), true)
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
