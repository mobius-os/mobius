import { test, beforeEach, afterEach, mock } from 'node:test'
import assert from 'node:assert/strict'

import {
  RESTART_INDICATOR_MAX_MS,
  clearRestartPending,
  getRestartPendingSnapshot,
  resetRestartStoreForTests,
  setRestartPending,
  subscribeRestart,
} from '../restartStore.js'

beforeEach(() => {
  resetRestartStoreForTests()
  mock.timers.enable({ apis: ['setTimeout'] })
})

afterEach(() => {
  mock.timers.reset()
  resetRestartStoreForTests()
})

test('starts clear', () => {
  assert.equal(getRestartPendingSnapshot(), false)
})

test('set marks pending and notifies once; a redundant set does not re-notify', () => {
  let notifications = 0
  subscribeRestart(() => { notifications += 1 })
  setRestartPending()
  assert.equal(getRestartPendingSnapshot(), true)
  assert.equal(notifications, 1)
  setRestartPending()
  assert.equal(notifications, 1, 'already-pending set is idempotent for subscribers')
})

test('clear resets pending and notifies once; a redundant clear does not re-notify', () => {
  let notifications = 0
  setRestartPending()
  subscribeRestart(() => { notifications += 1 })
  clearRestartPending()
  assert.equal(getRestartPendingSnapshot(), false)
  assert.equal(notifications, 1)
  clearRestartPending()
  assert.equal(notifications, 1, 'already-clear clear is idempotent for subscribers')
})

test('unsubscribe stops further notifications', () => {
  let notifications = 0
  const unsubscribe = subscribeRestart(() => { notifications += 1 })
  setRestartPending()
  unsubscribe()
  clearRestartPending()
  assert.equal(notifications, 1, 'no callback after unsubscribe')
})

test('the unconditional auto-expire clears a stuck indicator even with no other signal', () => {
  setRestartPending()
  mock.timers.tick(RESTART_INDICATOR_MAX_MS - 1)
  assert.equal(getRestartPendingSnapshot(), true, 'still pending just before the backstop')
  mock.timers.tick(1)
  assert.equal(getRestartPendingSnapshot(), false, 'auto-expired without a reconnect/ready')
})

test('a fresh restart signal re-arms the backstop instead of expiring on the first window', () => {
  setRestartPending()
  mock.timers.tick(RESTART_INDICATOR_MAX_MS - 1)
  setRestartPending() // second server_restarting extends the window
  mock.timers.tick(2) // past the ORIGINAL deadline, not the new one
  assert.equal(getRestartPendingSnapshot(), true, 're-armed, so the original timer no longer fires')
  mock.timers.tick(RESTART_INDICATOR_MAX_MS)
  assert.equal(getRestartPendingSnapshot(), false, 'new window elapses')
})

test('an explicit clear cancels the pending auto-expire (no late spurious fire)', () => {
  let notifications = 0
  setRestartPending()
  subscribeRestart(() => { notifications += 1 })
  clearRestartPending()
  assert.equal(notifications, 1)
  mock.timers.tick(RESTART_INDICATOR_MAX_MS * 2)
  assert.equal(notifications, 1, 'the cancelled timer never re-fires clear')
})
