import test from 'node:test'
import assert from 'node:assert/strict'

import {
  RESTART_UNTRACKED_MIN_WAIT_MS,
  restartCanReload,
} from '../restartReadiness.js'

test('restartCanReload waits for a changed boot id when both ids are known', () => {
  assert.equal(
    restartCanReload({ previousBootId: 'old', currentBootId: 'old', sawUnavailable: true }),
    false,
  )
  assert.equal(
    restartCanReload({ previousBootId: 'old', currentBootId: 'new' }),
    true,
  )
})

test('restartCanReload falls back to down cycle or conservative wait without boot ids', () => {
  assert.equal(restartCanReload({ elapsedMs: RESTART_UNTRACKED_MIN_WAIT_MS - 1 }), false)
  assert.equal(restartCanReload({ sawUnavailable: true }), true)
  assert.equal(restartCanReload({ elapsedMs: RESTART_UNTRACKED_MIN_WAIT_MS }), true)
})
