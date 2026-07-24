import test from 'node:test'
import assert from 'node:assert/strict'

import {
  RESTART_POLL_FAST_ATTEMPTS,
  RESTART_POLL_FAST_INTERVAL_MS,
  RESTART_POLL_MAX_ATTEMPTS,
  RESTART_POLL_SLOW_INTERVAL_MS,
  RESTART_UNTRACKED_MIN_WAIT_MS,
  restartCanReload,
  restartPollDecision,
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

test('restart polling keeps checking gently after the ordinary one-minute window', () => {
  assert.deepEqual(restartPollDecision(RESTART_POLL_FAST_ATTEMPTS - 1), {
    slow: false,
    timedOut: false,
    delayMs: RESTART_POLL_FAST_INTERVAL_MS,
  })
  assert.deepEqual(restartPollDecision(RESTART_POLL_FAST_ATTEMPTS), {
    slow: true,
    timedOut: false,
    delayMs: RESTART_POLL_SLOW_INTERVAL_MS,
  })
  assert.deepEqual(restartPollDecision(RESTART_POLL_MAX_ATTEMPTS), {
    slow: true,
    timedOut: true,
    delayMs: RESTART_POLL_SLOW_INTERVAL_MS,
  })
})
