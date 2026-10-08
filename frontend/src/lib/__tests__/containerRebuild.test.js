import assert from 'node:assert/strict'
import test from 'node:test'

import {
  rebuildAwaitingHostHelper,
  rebuildIsActive,
  rebuildPollShouldContinue,
  rebuildProgressMessage,
  rebuildRequestOutcome,
  rebuildStartedAgo,
  rebuildStatusLine,
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
  // Unresolved host ownership is not a running phase: do not spin forever.
  assert.equal(rebuildPollShouldContinue({ state: 'needs_recovery' }), false)
  assert.equal(rebuildRequestOutcome({ state: 'needs_recovery' }).terminalFailure, true)
})

test('container rebuild progress copy stays factual', () => {
  assert.equal(
    rebuildProgressMessage({ state: 'succeeded' }),
    'The updated system is ready.',
  )
  assert.equal(
    rebuildProgressMessage({ state: 'needs_recovery' }),
    'The replacement needs recovery before another update can start. Check Recovery in your deployment.',
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

test('only the unclaimed host request offers a withdraw action', () => {
  assert.equal(
    rebuildAwaitingHostHelper({ state: 'queued', code: 'host_helper_unclaimed' }),
    true,
  )
  assert.equal(
    rebuildAwaitingHostHelper({ state: 'failed', code: 'host_helper_unclaimed' }),
    false,
  )
  assert.equal(rebuildAwaitingHostHelper({ state: 'preparing' }), false)
})

test('elapsed time reads from the controller updated_at', () => {
  const now = Date.parse('2026-01-01T00:10:00Z')
  assert.equal(rebuildStartedAgo({ updated_at: '2026-01-01T00:09:30Z' }, now), 'just entered this stage')
  assert.equal(rebuildStartedAgo({ updated_at: '2026-01-01T00:07:00Z' }, now), 'in this stage for 3 min')
  assert.equal(rebuildStartedAgo({ updated_at: '2025-12-31T22:10:00Z' }, now), 'in this stage for about 2 hours')
  assert.equal(rebuildStartedAgo({ updated_at: '' }, now), '')
  assert.equal(rebuildStartedAgo({}, now), '')
})

test('active status line surfaces the real stage message and elapsed time', () => {
  const now = Date.parse('2026-01-01T00:05:00Z')
  assert.equal(
    rebuildStatusLine(
      { state: 'preparing', message: 'Selecting the verified Möbius image.', updated_at: '2026-01-01T00:02:00Z' },
      now,
    ),
    'Selecting the verified Möbius image. (in this stage for 3 min)',
  )
  // Falls back to fixed copy only when the controller sent no message.
  assert.equal(
    rebuildStatusLine({ state: 'preparing', updated_at: '2026-01-01T00:05:00Z' }, now),
    'Preparing the system update… (just entered this stage)',
  )
})

test('an unclaimed host request keeps the fixed line; its guidance is the description', () => {
  const now = Date.parse('2026-01-01T00:10:00Z')
  const line = rebuildStatusLine(
    {
      state: 'queued', code: 'host_helper_unclaimed',
      message: 'The host update helper has not picked up this request. Check it with `systemctl status mobius-rebuild.path` ...',
      updated_at: '2026-01-01T00:06:00Z',
    },
    now,
  )
  assert.equal(line, 'Preparing the system update… (in this stage for 4 min)')
})
