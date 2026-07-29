import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DRAWER_ROW_BATCH_SIZE,
  clampDrawerRowCount,
  initialDrawerRowCount,
  nextDrawerRowCount,
} from '../../components/Drawer/drawerProgressiveRows.js'


test('drawer starts with one bounded batch and grows continuously', () => {
  assert.equal(initialDrawerRowCount(0), 0)
  assert.equal(initialDrawerRowCount(12), 12)
  assert.equal(initialDrawerRowCount(426), DRAWER_ROW_BATCH_SIZE)
  assert.equal(
    nextDrawerRowCount(DRAWER_ROW_BATCH_SIZE, 426),
    DRAWER_ROW_BATCH_SIZE * 2,
  )
  assert.equal(nextDrawerRowCount(400, 426), 426)
})

test('drawer count survives reorder and clamps only when the list shrinks', () => {
  assert.equal(clampDrawerRowCount(144, 426), 144)
  assert.equal(clampDrawerRowCount(144, 80), 80)
  assert.equal(clampDrawerRowCount(12, 426), DRAWER_ROW_BATCH_SIZE)
})
