import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rebuildIsActive,
  rebuildNeedsBootstrap,
  rebuildPollShouldContinue,
  rebuildProgressMessage,
  rebuildRequestOutcome,
} from '../containerRebuild.js'

test('container rebuild active states are exactly the controller phases', () => {
  for (const state of [
    'queued', 'preparing', 'replacing', 'verifying',
  ]) {
    assert.equal(rebuildIsActive({ state }), true, state)
  }
  for (const state of [
    'idle', 'succeeded', 'no_change', 'failed', 'rolled_back', 'needs_recovery',
  ]) {
    assert.equal(rebuildIsActive({ state }), false, state)
  }
})

test('reviewed no-change is completion while standalone no-change stays informational', () => {
  assert.deepEqual(
    rebuildRequestOutcome({ state: 'no_change' }, { reviewedUpdate: true }),
    {
      state: 'no_change', accepted: true, cutoverAccepted: false,
      alreadyCurrent: true, terminalFailure: false,
    },
  )
  assert.deepEqual(
    rebuildRequestOutcome({ state: 'no_change' }),
    {
      state: 'no_change', accepted: false, cutoverAccepted: false,
      alreadyCurrent: false, terminalFailure: false,
    },
  )
  assert.equal(
    rebuildRequestOutcome({ state: 'rolled_back' }, { reviewedUpdate: true })
      .terminalFailure,
    true,
  )
})

test('legacy Railway status exposes the one-time bootstrap action', () => {
  assert.equal(rebuildNeedsBootstrap({ bootstrap_available: true }), true)
  assert.equal(rebuildNeedsBootstrap({ bootstrap_available: false }), false)
  assert.equal(
    rebuildProgressMessage({ bootstrap_available: true, state: 'succeeded' }),
    'Container updates are now enabled.',
  )
})

test('container rebuild polling survives transient status failures', () => {
  assert.equal(rebuildPollShouldContinue(null), true)
  assert.equal(rebuildPollShouldContinue({ state: 'replacing' }), true)
  assert.equal(rebuildPollShouldContinue({ state: 'succeeded' }), false)
  assert.equal(rebuildPollShouldContinue({ state: 'failed' }), false)
})

test('container rebuild progress copy stays factual', () => {
  assert.equal(
    rebuildProgressMessage({ state: 'succeeded' }),
    'Container rebuilt successfully.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'needs_recovery' }),
    'The container could not be restored. Use your deployment’s Recovery action.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'no_change', release_source: 'applied' }),
    'This container already matches the applied Möbius version.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'no_change', release_source: 'latest_ghcr' }),
    'This container already matches the latest official image.',
  )
})
