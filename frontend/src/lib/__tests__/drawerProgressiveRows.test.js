import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DRAWER_CHAT_BATCH_SIZE,
  clampDrawerChatCount,
  initialDrawerChatCount,
  nextDrawerChatCount,
} from '../../components/Drawer/drawerProgressiveRows.js'


test('drawer starts with one bounded batch and grows continuously', () => {
  assert.equal(initialDrawerChatCount(0), 0)
  assert.equal(initialDrawerChatCount(12), 12)
  assert.equal(initialDrawerChatCount(426), DRAWER_CHAT_BATCH_SIZE)
  assert.equal(
    nextDrawerChatCount(DRAWER_CHAT_BATCH_SIZE, 426),
    DRAWER_CHAT_BATCH_SIZE * 2,
  )
  assert.equal(nextDrawerChatCount(400, 426), 426)
})

test('drawer count survives reorder and clamps only when the list shrinks', () => {
  assert.equal(clampDrawerChatCount(144, 426), 144)
  assert.equal(clampDrawerChatCount(144, 80), 80)
  assert.equal(clampDrawerChatCount(12, 426), DRAWER_CHAT_BATCH_SIZE)
})
