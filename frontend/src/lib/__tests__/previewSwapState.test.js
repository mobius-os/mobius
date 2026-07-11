/**
 * Unit tests for lib/previewSwapState.js — the double-buffered preview swap
 * state machine.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/previewSwapState.test.js
 *
 * Pure reducer, no React/DOM — plain node:test. These lock in the invariants
 * that make a rebuild non-disruptive: the old frame survives until the new one
 * is proven mountable, a broken swap keeps the working frame, and at most one
 * extra frame ever exists.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  initSwapState,
  reduceSwap,
  compareVersions,
  INCOMING_SWAP_TIMEOUT_MS,
} from '../previewSwapState.js'

// Convenience: run a sequence of events from an initial version.
function run(version, events) {
  return events.reduce(reduceSwap, initSwapState(version))
}

test('init: starts on the given version, not loaded, no incoming, no swaps', () => {
  assert.deepEqual(initSwapState('100'), {
    liveVersion: '100', liveLoaded: false, incomingVersion: null, swaps: 0,
  })
})

test('a version event equal to the current live version is a no-op (idempotent prop re-render)', () => {
  const s = initSwapState('100')
  assert.equal(reduceSwap(s, { type: 'version', version: '100' }), s)
})

test('first-load settle: frame-mounted on the live version hides the overlay', () => {
  const s = run('100', [{ type: 'frame-mounted', version: '100' }])
  assert.equal(s.liveLoaded, true)
  assert.equal(s.incomingVersion, null)
  assert.equal(s.swaps, 0, 'first load is not a swap')
})

test('first-load error: frame-error on the live version hides overlay so the frame panel shows', () => {
  const s = run('100', [{ type: 'frame-error', version: '100' }])
  assert.equal(s.liveLoaded, true, 'overlay hidden so the iframe error panel is visible')
  assert.equal(s.swaps, 0)
})

test('a new version DURING first load retargets the single frame (no hidden second iframe)', () => {
  // Nothing has painted yet, so there is no continuity to protect.
  const s = run('100', [{ type: 'version', version: '101' }])
  assert.equal(s.liveVersion, '101')
  assert.equal(s.incomingVersion, null, 'must NOT spawn an incoming while still on first load')
  assert.equal(s.liveLoaded, false)
})

test('a new version AFTER load buffers a hidden incoming and keeps the live frame', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },   // loaded
    { type: 'version', version: '101' },         // bump
  ])
  assert.equal(s.liveVersion, '100', 'old frame stays live/visible')
  assert.equal(s.incomingVersion, '101', 'new version loads hidden alongside')
  assert.equal(s.liveLoaded, true, 'overlay never comes back for a bump')
})

test('promotion: incoming frame-mounted swaps it into view, drops the old frame, counts the swap', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'frame-mounted', version: '101' },   // incoming rendered
  ])
  assert.equal(s.liveVersion, '101', 'promoted to the new version')
  assert.equal(s.incomingVersion, null, 'old frame unmounted, no lingering incoming')
  assert.equal(s.liveLoaded, true)
  assert.equal(s.swaps, 1, 'one completed swap → one shimmer')
})

test('failed swap (frame-error from incoming): keep the OLD live frame, discard incoming', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'frame-error', version: '101' },     // broken bundle
  ])
  assert.equal(s.liveVersion, '100', 'the working version stays on screen')
  assert.equal(s.incomingVersion, null, 'the broken incoming is discarded')
  assert.equal(s.liveLoaded, true, 'no spinner — the owner is not stranded')
  assert.equal(s.swaps, 0, 'a failed swap does not count or shimmer')
})

test('failed swap by timeout: incoming-timeout keeps the old frame', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'incoming-timeout', version: '101' },
  ])
  assert.equal(s.liveVersion, '100')
  assert.equal(s.incomingVersion, null)
})

test('after a failed swap the next bump retries (old frame stayed the whole time)', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'frame-error', version: '101' },     // 101 broken → keep 100
    { type: 'version', version: '102' },         // agent fixed it
    { type: 'frame-mounted', version: '102' },   // 102 mounts
  ])
  assert.equal(s.liveVersion, '102')
  assert.equal(s.swaps, 1)
})

test('a newer version supersedes a still-loading incoming (only one extra frame)', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },   // incoming 101 loading
    { type: 'version', version: '102' },   // 102 arrives before 101 mounted
  ])
  assert.equal(s.liveVersion, '100', 'live untouched throughout')
  assert.equal(s.incomingVersion, '102', 'incoming replaced, not stacked')
})

test('a stale frame-mounted from a superseded incoming is ignored', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'version', version: '102' },        // 101 superseded by 102
    { type: 'frame-mounted', version: '101' },  // late mount from the dropped frame
  ])
  assert.equal(s.liveVersion, '100', 'a dropped incoming can NOT promote')
  assert.equal(s.incomingVersion, '102', 'still waiting on the current incoming')
  assert.equal(s.swaps, 0)
})

test('a stale incoming-timeout for a superseded version is ignored', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'version', version: '102' },
    { type: 'incoming-timeout', version: '101' },  // timer for the dropped 101
  ])
  assert.equal(s.incomingVersion, '102', 'the current incoming survives a stale timer')
})

test('reverting to the live version while an incoming is loading cancels the incoming', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },   // buffering 101
    { type: 'version', version: '100' },   // reverted back to what is live
  ])
  assert.equal(s.liveVersion, '100')
  assert.equal(s.incomingVersion, null, 'no reason to keep loading 101')
})

test('two successive swaps increment the swap counter', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'frame-mounted', version: '101' },
    { type: 'version', version: '102' },
    { type: 'frame-mounted', version: '102' },
  ])
  assert.equal(s.liveVersion, '102')
  assert.equal(s.swaps, 2)
})

test('reset returns a fresh first-load state', () => {
  const s = run('100', [
    { type: 'frame-mounted', version: '100' },
    { type: 'version', version: '101' },
    { type: 'reset', version: '500' },
  ])
  assert.deepEqual(s, { liveVersion: '500', liveLoaded: false, incomingVersion: null, swaps: 0 })
})

test('unknown event types are ignored (reducer stays total)', () => {
  const s = initSwapState('100')
  assert.equal(reduceSwap(s, { type: 'nope' }), s)
})

// compareVersions: deterministic + monotonic for digit-string version keys.
test('compareVersions: shorter digit string (smaller number) sorts first', () => {
  assert.ok(compareVersions('9', '100') < 0, "'9' < '100' numerically")
  assert.ok(compareVersions('100', '9') > 0)
})

test('compareVersions: equal-length compares lexicographically (== numeric for digits)', () => {
  assert.ok(compareVersions('100', '101') < 0)
  assert.equal(compareVersions('100', '100'), 0)
})

test('compareVersions: sorting a live+incoming pair is stable regardless of input order', () => {
  const a = ['1700000000000001', '1700000000000000']
  const b = ['1700000000000000', '1700000000000001']
  assert.deepEqual([...a].sort(compareVersions), [...b].sort(compareVersions))
})

test('INCOMING_SWAP_TIMEOUT_MS matches the frame init-timeout budget', () => {
  assert.equal(INCOMING_SWAP_TIMEOUT_MS, 10000)
})
