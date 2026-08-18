import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  goalObjectiveAtRunStart,
  goalObjectiveFromText,
  goalObjectiveFromRuntime,
  latestGoalObjective,
  progressRailViewModel,
  visibleGoalTasks,
} from '../goalProgress.js'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const streamConnection = readFileSync(
  new URL('../useStreamConnection.js', import.meta.url),
  'utf8',
)
const progressRail = readFileSync(new URL('../ProgressRail.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')

test('goalObjectiveFromText follows the backend command boundary', () => {
  assert.equal(goalObjectiveFromText('/goal Ship the review'), 'Ship the review')
  assert.equal(
    goalObjectiveFromText('\n/goal   Build the first slice\nthen verify it'),
    'Build the first slice then verify it',
  )
  assert.equal(goalObjectiveFromText('please /goal later'), '')
  assert.equal(goalObjectiveFromText(' /goal indented is prose'), '')
  assert.equal(goalObjectiveFromText('/data/apps/x'), '')
})

test('goalObjectiveFromText does not present clear or an empty command as active', () => {
  assert.equal(goalObjectiveFromText('/goal'), '')
  assert.equal(goalObjectiveFromText('/goal   '), '')
  assert.equal(goalObjectiveFromText('/goal clear'), '')
  assert.equal(goalObjectiveFromText('/goal CLEAR'), '')
})

test('latestGoalObjective recovers only the current visible owner turn', () => {
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal old objective' },
    { role: 'assistant', content: 'Done' },
    { role: 'user', content: 'ordinary follow-up' },
  ]), '')
  assert.equal(latestGoalObjective([
    { role: 'user', content: '/goal build the indicator' },
    { role: 'user', content: 'hidden answer', hidden: true },
    { role: 'assistant', content: 'Working', partial: true },
  ]), 'build the indicator')
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
  assert.equal(goalObjectiveFromRuntime({
    running: true,
    active_goal_objective: null,
  }, 'finish the migration'), 'finish the migration')
  assert.equal(goalObjectiveFromRuntime({
    running: true,
    active_goal_objective: 'authoritative goal',
  }, 'stale goal'), 'authoritative goal')
  assert.equal(goalObjectiveFromRuntime({
    running: false,
    active_goal_objective: null,
  }, 'finished goal'), '')
})

test('the goal reuses the progress rail and stays as context for build phases', () => {
  assert.deepEqual(
    progressRailViewModel('Build the indicator', []),
    [{
      key: 'goal',
      label: 'Goal · Build the indicator',
      current: true,
      expandable: true,
    }],
  )
  assert.deepEqual(
    progressRailViewModel('Build the indicator', [
      { ts: 1, label: 'First slice ready' },
      { ts: 2, label: 'Verifying' },
    ]),
    [
      {
        key: 'goal',
        label: 'Goal · Build the indicator',
        current: false,
        expandable: true,
      },
      { key: 'phase-1', label: 'First slice ready', current: false },
      { key: 'phase-2', label: 'Verifying', current: true },
    ],
  )
})

test('a planned goal shows every running branch and dependency progress', () => {
  const plan = {
    summary: { completed: 1, total: 4 },
    tasks: [
      { id: 'done', title: 'Inspect', status: 'completed' },
      { id: 'a', title: 'Run A', status: 'running', progress: { current: 2, total: 3 } },
      { id: 'b', title: 'Run B', status: 'running' },
      { id: 'c', title: 'Run C', status: 'pending', ready: false },
    ],
  }
  assert.deepEqual(visibleGoalTasks(plan).map(task => task.id), ['a', 'b'])
  assert.deepEqual(progressRailViewModel('Ship it', [], plan), [
    {
      key: 'goal',
      label: 'Goal · Ship it · 1/4',
      expandable: true,
      hasDetails: true,
      current: false,
    },
    {
      key: 'goal-task-a',
      label: 'Now · Run A · 2/3',
      goalTask: true,
      current: true,
    },
    {
      key: 'goal-task-b',
      label: 'Now · Run B',
      goalTask: true,
      current: true,
    },
  ])
})

test('a plan with no running work presents every independent ready task', () => {
  const plan = {
    summary: { completed: 0, total: 3 },
    tasks: [
      { id: 'a', title: 'A', status: 'pending', ready: true },
      { id: 'b', title: 'B', status: 'pending', ready: true },
      { id: 'c', title: 'C', status: 'pending', ready: false },
    ],
  }
  assert.deepEqual(
    visibleGoalTasks(plan).map(task => [task.id, task.activity]),
    [['a', 'Next'], ['b', 'Next']],
  )
})

test('ChatView binds goal state to explicit run boundaries, not transport liveness', () => {
  const runtimePoll = chatView.match(
    /const reconcileRuntimeState = useCallback[\s\S]*?const handleCompactionStored/,
  )?.[0] || ''
  assert.doesNotMatch(
    runtimePoll,
    /setServerRunningState|setActiveGoalState/,
    'one server snapshot must not publish through independent field setters',
  )
  assert.equal(
    runtimePoll.match(/updateChatRuntimeCache\(/g)?.length,
    1,
    'one server snapshot should publish one complete runtime cache patch',
  )
  const runStarts = chatView.split('setBuildPhases(railAtRunStart())').slice(1)
  assert.equal(runStarts.length, 4, 'every current run-start seam should be covered')
  for (const suffix of runStarts) {
    assert.match(
      suffix.slice(0, 380),
      /setActiveGoalState\(goalObjectiveAtRunStart\(/,
      'goal and build progress must reset together at each run boundary',
    )
  }
  assert.doesNotMatch(
    chatView,
    /if \(!turnActive\)[\s\S]{0,100}setActiveGoalState\(''\)/,
    'a transient loss of browser liveness must not retire a durable goal',
  )
  assert.match(
    chatView,
    /setServerRunningState\(false\)\s*setActiveGoalState\(''\)\s*\/\/ Stream ended without continuation/,
    'a terminal stream boundary must retire its goal indication',
  )
  assert.match(
    chatView,
    /disconnect\(\{ clearStreaming: true \}\)\s*promoteStreamToMessages\(\)\s*setSending\(false\)\s*setServerRunningLocalState\(false\)[\s\S]{0,500}setActiveGoalObjective\(''\)/,
    'a confirmed Stop must retire its goal indication',
  )
  assert.match(
    chatView,
    /onConnectionLost: \(\) => \{[\s\S]{0,500}promoteStreamToMessages\(\{ keepTurnOpen: true \}\)/,
    'connection loss may preserve partial output without ending the run',
  )
  assert.match(
    streamConnection,
    /onConnectionLostRef\.current\?\.\(\)[\s\S]{0,100}refreshThenSettleCatchUp\(\{ force: true \}\)/,
    'retry exhaustion must use the non-terminal handoff before reconciliation',
  )
  assert.match(
    chatView,
    /const visibleGoalObjective = activeGoalObjective/,
    'goal visibility must follow goal ownership rather than a transport flag',
  )
  assert.match(
    chatView,
    /activeGoalObjective: objective/,
    'the existing chat cache should retain a goal across chat switches and steers',
  )
  assert.match(
    chatView,
    /goalObjectiveFromRuntime\(\s*runtime,\s*latestGoalObjective\(visibleMessages\)/,
    'a cold chat read should restore the objective from the durable active run',
  )
  assert.match(
    chatView,
    /<ProgressRail\s+items=\{progressRail\}/,
    'the goal should render through the shared progress rail',
  )
  assert.match(
    chatView,
    /`Following goal: \$\{activeGoalObjective\}\.`/,
    'screen readers should receive the same active-goal status',
  )
  assert.match(progressRail, /chat__progress-rail/)
  assert.match(progressRail, /aria-expanded=\{expanded\}/)
  assert.match(progressRail, /label\.scrollWidth > step\.clientWidth/)
  assert.match(
    chatCss,
    /\.chat__foot \.chat__progress-step--toggle[\s\S]*?\{ pointer-events: auto; \}/,
    'an expandable step must opt back into pointer input inside the transparent footer',
  )
  assert.doesNotMatch(progressRail, /goal|build/i,
    'the shared rail should not encode one producer’s domain')
})
