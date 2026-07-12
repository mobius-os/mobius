import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  freshAppIds,
  withAppsFlagged,
  withoutAppFlagged,
} from '../newAppAttention.js'

test('freshAppIds returns only ids absent from the baseline', () => {
  const baseline = new Set([1, 2, 3])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3]), [])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3, 4]), [4])
  assert.deepEqual(freshAppIds(baseline, [5, 4]), [5, 4])
})

test('freshAppIds normalizes ids so a string route id does not double-count', () => {
  const baseline = new Set([7])
  assert.deepEqual(freshAppIds(baseline, ['7', 8]), [8])
  assert.deepEqual(freshAppIds([7], ['7']), [])
})

test('withAppsFlagged adds ids and keeps the same reference on a no-op', () => {
  const prev = new Set([1])
  const added = withAppsFlagged(prev, [2, 3])
  assert.deepEqual([...added], [1, 2, 3])

  assert.equal(withAppsFlagged(prev, []), prev)
  assert.equal(withAppsFlagged(prev, [1]), prev)
})

test('withoutAppFlagged clears one id and no-ops when absent', () => {
  const prev = new Set([1, 2])
  const cleared = withoutAppFlagged(prev, 2)
  assert.deepEqual([...cleared], [1])

  assert.equal(withoutAppFlagged(prev, 9), prev)
  assert.equal(withoutAppFlagged(prev, '2') === prev, false)
  assert.deepEqual([...withoutAppFlagged(prev, '2')], [1])
})
