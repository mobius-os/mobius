import test from 'node:test'
import assert from 'node:assert/strict'
import { formatGoalDuration, goalHistoryViewModel } from '../goalHistory.js'

test('Goal history formats useful elapsed time compactly', () => {
  assert.equal(formatGoalDuration(14.6), '15s')
  assert.equal(formatGoalDuration(125), '2m 5s')
  assert.equal(formatGoalDuration(7260), '2h 1m')
})

test('Goal history summarizes a terminal outcome and its plan', () => {
  assert.deepEqual(goalHistoryViewModel({
    objective: ' Ship the Goal experience ',
    status: 'completed',
    duration_seconds: 125,
    plan: {
      summary: { completed: 3, total: 3 },
      tasks: [{ id: 'verify', title: 'Verify', status: 'completed' }],
    },
  }), {
    objective: 'Ship the Goal experience',
    completed: true,
    kicker: 'Goal completed',
    ariaLabel: 'Completed goal: Ship the Goal experience',
    metadata: '3 of 3 steps complete · 2m 5s',
    hasPlan: true,
  })
})

test('Goal history rejects active snapshots and labels failed outcomes', () => {
  assert.equal(goalHistoryViewModel({ objective: 'Still working', status: 'active' }), null)
  assert.deepEqual(goalHistoryViewModel({
    objective: 'Needs repair', status: 'failed', duration_seconds: null,
  }), {
    objective: 'Needs repair',
    completed: false,
    kicker: 'Goal needs attention',
    ariaLabel: 'Goal needing attention: Needs repair',
    metadata: '',
    hasPlan: false,
  })
})
