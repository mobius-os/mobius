import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  compactGoalObjective,
  goalObjectiveForQueuedStart,
  goalObjectiveAtRunStart,
  goalObjectiveFromText,
  goalObjectiveFromRuntime,
  goalMessageObjectiveFromText,
  goalTaskDisplayStatus,
  latestGoalObjective,
  newestGoalPlan,
  progressRailViewModel,
  visibleGoalTasks,
} from '../goalProgress.js'

const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const streamConnection = readFileSync(
  new URL('../useStreamConnection.js', import.meta.url),
  'utf8',
)
const progressRail = readFileSync(new URL('../ProgressRail.jsx', import.meta.url), 'utf8')
const goalPlanDetails = readFileSync(
  new URL('../GoalPlanDetails.jsx', import.meta.url),
  'utf8',
)
const msgContent = readFileSync(new URL('../MsgContent.jsx', import.meta.url), 'utf8')
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
  assert.equal(goalObjectiveFromText('/goal\nShip after review'), 'Ship after review')
})

test('goalObjectiveFromText does not present clear or an empty command as active', () => {
  assert.equal(goalObjectiveFromText('/goal'), '')
  assert.equal(goalObjectiveFromText('/goal   '), '')
  assert.equal(goalObjectiveFromText('/goal clear'), '')
  assert.equal(goalObjectiveFromText('/goal CLEAR'), '')
})

test('goal owner messages hide only a real command token and preserve objective formatting', () => {
  assert.equal(
    goalMessageObjectiveFromText('/goal Build the first slice\nthen verify it'),
    'Build the first slice\nthen verify it',
  )
  assert.equal(goalMessageObjectiveFromText('please /goal later'), '')
  assert.equal(goalMessageObjectiveFromText('/goal clear'), '')
  assert.match(msgContent, /<UserMessageText text=\{text\} \/>/)
  assert.match(msgContent, /className="chat__goal-message-tag" aria-hidden="true">Goal<\/span>/)
  assert.match(msgContent, /className="chat__sr-only">Goal: <\/span>/)
  assert.match(chatCss, /\.chat__goal-message\s*\{[\s\S]*?display: inline;/)
  assert.match(chatCss, /\.chat__goal-message-tag\s*\{[\s\S]*?display: inline-block;/)
})

test('newestGoalPlan rejects a stale fetch without hiding a new logical goal', () => {
  const current = { root_run_id: 'root-a', revision: 3 }
  assert.equal(newestGoalPlan(current, null), current)
  assert.equal(
    newestGoalPlan(current, { root_run_id: 'root-a', revision: 2 }),
    current,
  )

  const newer = { root_run_id: 'root-a', revision: 4 }
  assert.equal(newestGoalPlan(current, newer), newer)

  const newGoal = { root_run_id: 'root-b', revision: 1 }
  assert.equal(newestGoalPlan(current, newGoal), newGoal)
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
  assert.equal(goalObjectiveForQueuedStart({
    content: '<delegation_results>[]</delegation_results>',
    hidden: true,
    kind: 'delegation_result',
    _goal_objective: 'Ship the audited result',
  }, []), 'Ship the audited result')
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
    running: true,
    active_goal_objective: 'Review every issue\nthen verify the result',
  }), 'Review every issue then verify the result')
  assert.equal(goalObjectiveFromRuntime({
    running: false,
    active_goal_objective: null,
  }, 'finished goal'), '')
  assert.equal(goalObjectiveFromRuntime({
    running: false,
    active_goal_objective: 'Waiting for delegated checks',
  }, 'stale goal'), 'Waiting for delegated checks')
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

test('stale plan data cannot show tasks after the active goal has ended', () => {
  const plan = {
    tasks: [{ id: 'old', title: 'Old work', status: 'running' }],
  }
  assert.deepEqual(progressRailViewModel('', [], plan), [])
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
      label: 'Goal · 1/4 · Run A · 2/3 + Run B',
      expandable: true,
      title: 'Goal: Ship it',
      ariaLabel: 'Goal for Ship it; 1 of 4 complete',
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
    visibleGoalTasks(plan).map(task => task.id),
    ['a', 'b'],
  )
})

test('the deepest live delegated owners replace their parent in the collapsed label', () => {
  const plan = {
    tasks: [{ id: 'b', title: 'Do B', status: 'running' }],
    delegations: [{
      id: 'delegation-b', task_key: 'b', status: 'running', children: [
        { id: 'delegation-x', task_key: 'x', status: 'running', children: [] },
        { id: 'delegation-y', task_key: 'y', status: 'running', children: [] },
      ],
    }],
  }
  assert.deepEqual(
    visibleGoalTasks(plan).map(task => task.title),
    ['X', 'Y'],
  )
})

test('delegated leaves stay beside independent local running work', () => {
  const plan = {
    tasks: [
      { id: 'a', title: 'Delegated A', status: 'running' },
      { id: 'b', title: 'Local B', status: 'running' },
    ],
    delegations: [{
      id: 'delegation-a', task_key: 'a', status: 'running', children: [
        { id: 'delegation-x', task_key: 'x', status: 'running', children: [] },
      ],
    }],
  }
  assert.deepEqual(
    visibleGoalTasks(plan).map(task => task.title),
    ['Local B', 'X'],
  )
  const completedAncestors = {
    tasks: [
      { id: 'audit', title: 'Audit', status: 'running' },
      { id: 'other', title: 'Other', status: 'running' },
    ],
    delegations: [{
      id: 'delegation-audit', task_key: 'audit', status: 'completed', children: [{
        id: 'delegation-detail', task_key: 'detail', status: 'completed', children: [{
          id: 'delegation-leaf', task_key: 'leaf-check', status: 'running', children: [],
        }],
      }],
    }],
  }
  assert.deepEqual(
    visibleGoalTasks(completedAncestors).map(task => task.title),
    ['Other', 'Leaf check'],
  )
})

test('the deepest running plan nodes replace coordinating parents', () => {
  const plan = {
    tasks: [
      { id: 'root', title: 'Coordinate', status: 'running' },
      { id: 'branch', parent_id: 'root', title: 'Inspect branch', status: 'running' },
      { id: 'leaf', parent_id: 'branch', title: 'Verify leaf', status: 'running' },
      { id: 'parallel', parent_id: 'root', title: 'Check parallel path', status: 'running' },
    ],
  }
  assert.deepEqual(
    visibleGoalTasks(plan).map(task => task.id),
    ['leaf', 'parallel'],
  )
})

test('malformed parent cycles cannot hang current-work selection', () => {
  const plan = {
    tasks: [
      { id: 'a', parent_id: 'b', title: 'A', status: 'running' },
      { id: 'b', parent_id: 'a', title: 'B', status: 'running' },
    ],
  }
  assert.deepEqual(visibleGoalTasks(plan), [])
})

test('malformed delegation cycles cannot recurse forever', () => {
  const a = { id: 'a', task_key: 'a', status: 'running', children: [] }
  const b = { id: 'b', task_key: 'b', status: 'running', children: [a] }
  a.children = [b]
  assert.deepEqual(
    visibleGoalTasks({ delegations: [a] }).map(task => task.id),
    ['b'],
  )
})

test('live delegated execution outranks a stale completed task presentation', () => {
  const task = { id: 'audit', status: 'completed' }
  assert.equal(goalTaskDisplayStatus(task, { status: 'running' }), 'running')
  assert.equal(goalTaskDisplayStatus(task, { status: 'paused' }), 'running')
  assert.equal(goalTaskDisplayStatus(task, { status: 'needs_review' }), 'failed')
  assert.equal(goalTaskDisplayStatus(task, { status: 'completed' }), 'completed')
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
      /setActiveGoalState\(goalObjective(?:AtRunStart|ForQueuedStart)\(/,
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
    /setServerRunningState\(false\)[\s\S]{0,180}activeGoalPlan\?\.summary\?\.can_complete !== false[\s\S]{0,100}setActiveGoalState\(''\)/,
    'a terminal stream should retain an unfinished delegated Goal but retire a settled one',
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
    /activeGoalObjective: compactObjective/,
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
  assert.doesNotMatch(
    chatView,
    /<ProgressRail[\s\S]{0,160}\skey=/,
    'late plan data must not remount the rail and replay its entrance animation',
  )
  assert.match(progressRail, /useEffect\(\(\) => setDetailsKey\(null\), \[resetKey\]\)/)
  assert.match(
    chatView,
    /`Following goal: \$\{activeGoalObjective\}\.`/,
    'screen readers should receive the same active-goal status',
  )
  assert.match(progressRail, /chat__progress-rail/)
  assert.match(progressRail, /aria-expanded=\{expanded\}/)
  assert.doesNotMatch(progressRail, /chat__progress-step-action/)
  assert.doesNotMatch(progressRail, /expandedActionLabel/)
  assert.doesNotMatch(progressRail, /ResizeObserver|scrollWidth|clientWidth/)
  assert.doesNotMatch(
    chatCss,
    /\.chat__progress-step--button:hover/,
    'the Goal header should toggle without a selected-looking hover fill',
  )
  assert.match(progressRail, /aria-label=\{`\$\{expanded \? 'Collapse' : 'Expand'\}/)
  assert.match(
    chatCss,
    /\.chat__foot \.chat__progress-step--toggle[\s\S]*?\{ pointer-events: auto; \}/,
    'an expandable step must opt back into pointer input inside the transparent footer',
  )
  assert.doesNotMatch(progressRail, /goal|build/i,
    'the shared rail should not encode one producer’s domain')
  assert.match(
    goalPlanDetails,
    /className="chat__goal-branch" role="listitem"[\s\S]*?role="list"/,
    'expanded Goal hierarchy should expose nested list semantics',
  )
  assert.match(
    goalPlanDetails,
    /execution = delegatedByTask\.get\(task\.id\)/,
    'a nested plan task without a child-local match should retain root execution fallback',
  )
  assert.match(
    goalPlanDetails,
    /status=\{goalTaskDisplayStatus\(task, execution\)\}/,
    'live delegated execution should own the row presentation state',
  )
})
