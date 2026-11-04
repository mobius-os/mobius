import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  createImmersiveLifecycle,
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
  // A different canvas or a first mount is never a return.
  assert.equal(immersiveLifecycleValue(null, current, true, true), null)
  assert.equal(immersiveLifecycleValue({ ...previous, appId: 3 }, current, true, true), null)
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

// createImmersiveLifecycle is what AppCanvas calls from its message handler
// (frameRequested), its lifecycle effect (transition) and its frame-visibility
// effect (frameHidden). These sequences mirror those calls.
const at = (liveVersion, active) => ({ appId: 7, liveVersion, active })
const post = (lifecycle, { version = 'a', liveVersion = 'a', active, frameVisible }) =>
  lifecycle.frameRequested({ version, liveVersion, active, frameVisible })

test('only the live frame of the active canvas is forwarded in real time', () => {
  const lifecycle = createImmersiveLifecycle()
  assert.equal(post(lifecycle, { active: true, frameVisible: true }), true)
  assert.equal(post(lifecycle, { version: 'b', active: true, frameVisible: true }), false)
  assert.equal(post(lifecycle, { active: false, frameVisible: true }), false)
  assert.equal(post(lifecycle, { active: false, frameVisible: false }), false)
})

test('a request made while returning is honoured once, on activation', () => {
  const lifecycle = createImmersiveLifecycle()
  assert.equal(lifecycle.transition(at('a', true), undefined), null)
  assert.equal(lifecycle.transition(at('a', false), undefined), false)
  // Shown again, not yet active: the app asks as it reappears.
  post(lifecycle, { active: false, frameVisible: true })
  assert.equal(lifecycle.transition(at('a', true), true), true)
  // The next visit makes no fresh request, so nothing is resurrected.
  assert.equal(lifecycle.transition(at('a', false), true), false)
  assert.equal(lifecycle.transition(at('a', true), true), null)
})

test('a request posted during the chat handoff never replays on a later return', () => {
  // Shell keeps the leaving frame painted (frameVisible) after the pane went
  // inactive. A post in that window must die with that showing.
  const lifecycle = createImmersiveLifecycle()
  lifecycle.transition(at('a', true), undefined)
  assert.equal(lifecycle.transition(at('a', false), true), false)
  post(lifecycle, { active: false, frameVisible: true })
  lifecycle.frameHidden()
  assert.equal(lifecycle.transition(at('a', true), true), null)
})

test('an unfocused split pane request is honoured on focus unless withdrawn', () => {
  const lifecycle = createImmersiveLifecycle()
  assert.equal(lifecycle.transition(at('a', false), undefined), false)
  post(lifecycle, { active: false, frameVisible: true })
  assert.equal(lifecycle.transition(at('a', true), true), true)

  // Focus moves away and back; the app asks, then withdraws, while unfocused.
  lifecycle.transition(at('a', false), true)
  post(lifecycle, { active: false, frameVisible: true })
  post(lifecycle, { active: false, frameVisible: true })
  assert.equal(lifecycle.transition(at('a', true), false), null)
})

test('a hidden incoming frame post is not taken as a return request', () => {
  const lifecycle = createImmersiveLifecycle()
  lifecycle.transition(at('a', false), undefined)
  post(lifecycle, { version: 'b', liveVersion: 'a', active: false, frameVisible: true })
  assert.equal(lifecycle.transition(at('b', true), true), null)
})

test('a request from a frame that was since replaced is discarded', () => {
  const lifecycle = createImmersiveLifecycle()
  lifecycle.transition(at('a', false), undefined)
  post(lifecycle, { active: false, frameVisible: true })
  // Promotion while still inactive is itself a transition and consumes it.
  assert.equal(lifecycle.transition(at('b', false), undefined), false)
  assert.equal(lifecycle.transition(at('b', true), true), null)
})
