import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rebuildIsActive,
  rebuildPollShouldContinue,
  rebuildProgressMessage,
} from '../containerRebuild.js'

test('container rebuild active states are exactly the controller phases', () => {
  for (const state of [
    'queued', 'preparing', 'waiting_for_work', 'replacing', 'verifying',
  ]) {
    assert.equal(rebuildIsActive({ state }), true, state)
  }
  for (const state of [
    'idle', 'succeeded', 'no_change', 'failed', 'rolled_back', 'needs_recovery',
  ]) {
    assert.equal(rebuildIsActive({ state }), false, state)
  }
})

test('container rebuild polling survives transient status failures', () => {
  assert.equal(rebuildPollShouldContinue(null), true)
  assert.equal(rebuildPollShouldContinue({ state: 'replacing' }), true)
  assert.equal(rebuildPollShouldContinue({ state: 'succeeded' }), false)
  assert.equal(rebuildPollShouldContinue({ state: 'failed' }), false)
})

test('container rebuild progress copy stays provider-neutral', () => {
  assert.equal(
    rebuildProgressMessage({ state: 'waiting_for_work' }),
    'Waiting for active work to finish…',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'needs_recovery' }),
    'The container could not be restored. Use your deployment’s Recovery action.',
  )
})
