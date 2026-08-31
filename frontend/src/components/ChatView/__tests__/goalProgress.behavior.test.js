import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  compactGoalObjective,
  goalObjectiveAtRunStart,
  goalObjectiveForQueuedStart,
  goalPresentationAtRunStart,
  goalPresentationForQueuedStart,
  goalPresentationFromRuntime,
  latestGoalObjective,
} from '../goalProgress.js'

test('compact Goal objectives are canonical before the first paint', () => {
  assert.equal(
    compactGoalObjective('Review every issue\nthen verify the result'),
    'Review every issue then verify the result',
  )
  assert.equal(compactGoalObjective(null), '')
})

test('a resumable continue keeps the same goal through live start and cold attach', () => {
  const interruptedGoal = [
    { role: 'user', content: '/goal finish the migration' },
    {
      role: 'assistant',
      content: 'Partly done',
      blocks: [{
        type: 'error',
        message: 'Interrupted',
        resumable: true,
      }],
    },
  ]
  assert.equal(
    goalObjectiveAtRunStart('continue', interruptedGoal),
    'finish the migration',
  )
  assert.equal(
    latestGoalObjective([
      ...interruptedGoal,
      { role: 'user', content: 'continue' },
      { role: 'assistant', content: 'Working', partial: true },
    ]),
    'finish the migration',
  )
})

test('a queued child-result continuation keeps the committed Goal without jitter', () => {
  const message = {
    content: '<delegation_results>[]</delegation_results>',
    hidden: true,
    kind: 'delegation_result',
    _goal_objective: 'Ship the audited result',
  }
  assert.equal(
    goalObjectiveForQueuedStart(message, []),
    'Ship the audited result',
  )
  assert.deepEqual(
    goalPresentationForQueuedStart(message, [], {
      id: 'settled-goal', objective: 'Old goal', status: 'completed',
    }),
    {
      id: null,
      objective: 'Ship the audited result',
      status: 'active',
      resumable: false,
    },
  )
})

test('an ordinary queued turn retains a settled Goal presentation', () => {
  const completed = {
    id: 'goal-1', objective: 'Finish the migration', status: 'completed',
  }
  assert.deepEqual(
    goalPresentationForQueuedStart({
      content: 'ordinary follow-up',
      _goal_objective: null,
    }, [], completed),
    { ...completed, resumable: false },
  )
})

test('continuation recovery preserves only an active goal', () => {
  assert.equal(goalObjectiveAtRunStart('continue', [
    { role: 'user', content: '/goal old objective' },
    { role: 'assistant', content: 'Done' },
  ]), '')
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal old objective' },
    { role: 'assistant', blocks: [{ type: 'error', resumable: true }] },
    { role: 'user', content: '/goal clear' },
    { role: 'assistant', blocks: [{ type: 'error', resumable: true }] },
    { role: 'user', content: 'continue' },
  ]), '')
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal old objective' },
    { role: 'assistant', blocks: [{ type: 'error', resumable: true }] },
    { role: 'user', content: 'new subject' },
    { role: 'assistant', blocks: [{ type: 'error', resumable: true }] },
    { role: 'user', content: 'continue' },
  ]), '')
})

test('durable Goal presentation survives terminal runtime states', () => {
  const paused = {
    id: 'goal-1', objective: 'Finish the migration', status: 'paused',
    resumable: true,
  }
  assert.deepEqual(goalPresentationFromRuntime({
    running: false,
    goal: paused,
  }), paused)
  assert.deepEqual(goalPresentationFromRuntime({
    running: false,
    goal: {
      id: 'goal-1', objective: 'Finish the migration', status: 'completed',
    },
  }), {
    id: 'goal-1', objective: 'Finish the migration', status: 'completed',
    resumable: false,
  })
  assert.equal(goalPresentationFromRuntime({ running: false, goal: null }), null)
})

test('ordinary turns retain settled Goals while Resume reactivates a pause', () => {
  const paused = {
    id: 'goal-1', objective: 'Finish the migration', status: 'paused',
  }
  assert.deepEqual(
    goalPresentationAtRunStart('ordinary question', [], paused),
    { ...paused, resumable: true },
  )
  assert.deepEqual(
    goalPresentationAtRunStart('continue', [
      { role: 'user', content: '/goal Finish the migration' },
      { role: 'assistant', blocks: [{ type: 'error', resumable: true }] },
    ], paused),
    { ...paused, status: 'active', resumable: false },
    'Resume preserves the durable Goal identity even when transcript parsing also finds it',
  )
  assert.deepEqual(
    goalPresentationAtRunStart('/goal Start another', [], paused),
    { id: null, objective: 'Start another', status: 'active', resumable: false },
  )
  assert.deepEqual(
    goalPresentationAtRunStart('/goal clear', [], paused),
    { ...paused, resumable: true },
    'the retired text command must not optimistically hide a durable Goal',
  )
})

test('a promoted continuation preserves the matching retained Goal identity', () => {
  const paused = {
    id: 'goal-1', objective: 'Finish the migration', status: 'paused',
  }
  assert.deepEqual(goalPresentationForQueuedStart({
    content: '<wait_result />',
    _goal_objective: 'Finish the migration',
  }, [], paused), {
    ...paused,
    status: 'active',
    resumable: false,
  })
})
