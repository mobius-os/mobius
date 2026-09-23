import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rebuildIsActive,
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

test('container rebuild polling survives transient status failures', () => {
  assert.equal(rebuildPollShouldContinue(null), true)
  assert.equal(rebuildPollShouldContinue({ state: 'replacing' }), true)
  assert.equal(rebuildPollShouldContinue({ state: 'succeeded' }), false)
  assert.equal(rebuildPollShouldContinue({ state: 'failed' }), false)
})

test('container rebuild progress copy stays factual', () => {
  assert.equal(
    rebuildProgressMessage({ state: 'succeeded' }),
    'The updated system is ready.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'needs_recovery' }),
    'Möbius could not return to the previous version. Use Recovery in your deployment.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'no_change', release_source: 'applied' }),
    'Möbius already uses the installed update.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'no_change', release_source: 'latest_ghcr' }),
    'Möbius already uses the latest official version.',
  )
})
