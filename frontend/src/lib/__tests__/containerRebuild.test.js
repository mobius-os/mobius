import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rebuildIsActive,
  rebuildNeedsBootstrap,
  rebuildPollShouldContinue,
  rebuildProgressMessage,
} from '../containerRebuild.js'

test('container replacement active states are exactly the controller phases', () => {
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

test('legacy Railway status exposes the one-time bootstrap action', () => {
  assert.equal(rebuildNeedsBootstrap({ bootstrap_available: true }), true)
  assert.equal(rebuildNeedsBootstrap({ bootstrap_available: false }), false)
  assert.equal(
    rebuildProgressMessage({ bootstrap_available: true, state: 'succeeded' }),
    'Container updates are now enabled.',
  )
})

test('container replacement polling survives transient status failures', () => {
  assert.equal(rebuildPollShouldContinue(null), true)
  assert.equal(rebuildPollShouldContinue({ state: 'replacing' }), true)
  assert.equal(rebuildPollShouldContinue({ state: 'succeeded' }), false)
  assert.equal(rebuildPollShouldContinue({ state: 'failed' }), false)
})

test('container replacement progress copy stays factual', () => {
  assert.equal(
    rebuildProgressMessage({ state: 'succeeded' }),
    'Container replaced successfully.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'needs_recovery' }),
    'The container could not be restored. Use your deployment’s Recovery action.',
  )
})
