import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  compactGoalObjective,
  draftGoalObjective,
  goalObjectiveAtRunStart,
  goalObjectiveFromText,
  goalMessageObjectiveFromText,
  goalPresentationAtRunStart,
  goalPresentationFromRuntime,
  goalTaskDisplayStatus,
  latestGoalObjective,
  newestGoalPlan,
  normalizeGoalPresentation,
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

test('durable Goal presentation preserves only exact server-owned waits', () => {
  assert.deepEqual(normalizeGoalPresentation({
    id: 'goal-1', objective: 'Finish the review', status: 'paused',
    wait_kind: 'monitor',
  }), {
    id: 'goal-1', objective: 'Finish the review', status: 'paused',
    resumable: true, wait_kind: 'monitor',
  })
  assert.deepEqual(normalizeGoalPresentation({
    id: 'goal-1', objective: 'Finish the review', status: 'paused',
    wait_kind: 'unrelated-browser-state',
  }), {
    id: 'goal-1', objective: 'Finish the review', status: 'paused',
    resumable: true,
  })
  assert.match(
    chatView,
    /ownerActionRequired: goalPresentation\?\.wait_kind === 'owner_question'/,
  )
  assert.match(
    chatView,
    /monitoring: goalPresentation\?\.wait_kind === 'monitor'/,
  )
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
    goalPresentationAtRunStart('continue', [], paused),
    { ...paused, status: 'active', resumable: false },
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

test('the goal reuses the progress rail and stays as context for build phases', () => {
  assert.deepEqual(
    progressRailViewModel('Build the indicator', []),
    [{
      key: 'goal',
      label: 'Goal · Build the indicator',
      current: true,
      expandable: true,
      tone: 'active',
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
        tone: 'active',
      },
      { key: 'phase-1', label: 'First slice ready', current: false },
      { key: 'phase-2', label: 'Verifying', current: true },
    ],
  )
})

test('only actionable Goals remain in the composer progress rail', () => {
  const plan = {
    summary: { completed: 2, total: 3 },
    tasks: [{ id: 'verify', title: 'Verify', status: 'running' }],
  }
  assert.equal(progressRailViewModel({
    objective: 'Ship it', status: 'paused',
  }, [], plan)[0].label, 'Goal · Paused · 2/3 · Verify')
  assert.deepEqual(progressRailViewModel({
    objective: 'Ship it', status: 'completed',
  }, [], {
    ...plan,
    summary: { completed: 3, total: 3 },
  }), [])
  assert.deepEqual(progressRailViewModel({
    objective: 'Ship it', status: 'failed',
  }, [], plan), [])
})

test('the Goal rail names who owns an unfinished wait', () => {
  const goal = { objective: 'Ship it', status: 'paused' }
  const plan = {
    summary: { completed: 2, total: 3 },
    tasks: [{ id: 'verify', title: 'Verify', status: 'running' }],
  }
  const ownerWait = progressRailViewModel(goal, [], plan, {
    ownerActionRequired: true,
    monitoring: true,
  })[0]
  assert.equal(ownerWait.label, 'Goal · Waiting for you · 2/3 · Verify')
  assert.equal(
    ownerWait.ariaLabel,
    'Goal waiting for you for Ship it; 2 of 3 complete',
  )

  const monitoredWait = progressRailViewModel(goal, [], plan, {
    monitoring: true,
  })[0]
  assert.equal(monitoredWait.label, 'Goal · Monitoring · 2/3 · Verify')
  assert.equal(
    monitoredWait.ariaLabel,
    'Goal monitoring for Ship it; 2 of 3 complete',
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
      ariaLabel: 'Goal active for Ship it; 1 of 4 complete',
      tone: 'active',
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

test('ChatView retains settled goals independently of transport liveness', () => {
  const runtimePoll = chatView.match(
    /const refreshRuntimeState = useCallback[\s\S]*?const reconcileRuntimeState/,
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
      /setGoalAtRunStart\(/,
      'goal and build progress must reconcile together at each run boundary',
    )
  }
  assert.doesNotMatch(
    chatView,
    /if \(!turnActive\)[\s\S]{0,100}setActiveGoalState\(''\)/,
    'a transient loss of browser liveness must not retire a durable goal',
  )
  assert.doesNotMatch(
    chatView,
    /setGoalState\(\{ \.\.\.endingGoal, status: 'completed' \}\)/,
    'a physical stream ending must not claim Goal completion before server confirmation',
  )
  assert.match(
    chatView,
    /if \(endingGoal \|\| pendingQueue\.pendingMessagesRef\.current\.length > 0\) \{\s*fetchMessages\(\{ force: true, authoritative: true \}\)/,
    'the existing authoritative refresh owns completed versus paused Goal status',
  )
  assert.match(
    chatView,
    /disconnect\(\{ clearStreaming: true \}\)\s*promoteStreamToMessages\(\)\s*setSending\(false\)\s*setServerRunningLocalState\(false\)[\s\S]{0,700}status: 'paused'/,
    'a confirmed Stop must preserve the goal as paused',
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
    /goal: normalized/,
    'the existing chat cache should retain a goal across chat switches and steers',
  )
  assert.match(
    chatView,
    /goalPresentationFromRuntime\(\s*runtime,/,
    'a cold chat read should restore the durable Goal presentation',
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
  assert.match(
    chatCss,
    /\.chat__foot \.chat__progress-step--toggle,[\s\S]*?\.chat__foot \.chat__progress-action,[\s\S]*?\.chat__foot \.chat__progress-clear,[\s\S]*?\{ pointer-events: auto; \}/,
    'the Goal action siblings must opt back into pointer input with the expandable step',
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

test('draftGoalObjective keeps the goal visual open while typing the objective', () => {
  // Requires the whitespace that dismisses the slash menu, so the chip takes
  // over exactly as the picker closes — a bare `/goal` still belongs to it.
  assert.equal(draftGoalObjective('/goal'), null)
  assert.equal(draftGoalObjective('/goalise the plan'), null)
  // The space-only draft arms the chip with no objective yet.
  assert.equal(draftGoalObjective('/goal '), '')
  assert.equal(draftGoalObjective('/goal Ship the review'), 'Ship the review')
  assert.equal(
    draftGoalObjective('/goal   Build the first slice\nthen verify'),
    'Build the first slice then verify',
  )
  // A leading-newline draft still counts (backend tolerates leading newlines).
  assert.equal(draftGoalObjective('\n/goal Ship it'), 'Ship it')
  // Not a goal command → no chip.
  assert.equal(draftGoalObjective('please /goal later'), null)
  assert.equal(draftGoalObjective(' /goal indented is prose'), null)
  assert.equal(draftGoalObjective('/data/apps/x'), null)
  // `/goal clear` is a control phrase, not a new objective.
  assert.equal(draftGoalObjective('/goal clear'), null)
  assert.equal(draftGoalObjective('/goal  clear'), null)
  // A near-miss like "clearance" is still a real objective.
  assert.equal(draftGoalObjective('/goal clearance sale'), 'clearance sale')
  assert.equal(draftGoalObjective(null), null)
})

test('the goal rail confirms and clears directly, sourced domain-neutrally', () => {
  assert.match(
    chatView,
    /handleClearGoal\s*=\s*useCallback\(async \(item\)[\s\S]*?apiFetch\(`\/chats\/\$\{chatId\}\/goal`[\s\S]*?method:\s*'DELETE'[\s\S]*?goal_id:\s*goalId/,
    'the confirmed clear must call the exact-id lifecycle route directly',
  )
  assert.doesNotMatch(
    chatView,
    /doSend\('\/goal clear'/,
    'the Goal rail must never fabricate a /goal clear chat message',
  )
  assert.match(chatView, /clearable:\s*!!actionableGoalPresentation\?\.id/,
    'only an identified durable Goal may expose clearing')
  assert.match(chatView, /onClearItem=\{handleClearGoal\}/,
    'ChatView must wire the clear handler into the rail')
  // The rail itself stays domain-neutral: the label text comes from item data.
  assert.match(progressRail, /className=\{`chat__progress-clear\$\{clearConfirmed/,
    'the rail must render the clear button')
  assert.match(progressRail, /if \(clearConfirmed\) onClear\(item\)[\s\S]*?else setClearConfirmed\(true\)/,
    'the first click must arm confirmation and only the second may clear')
  assert.match(progressRail, /clearConfirmed[\s\S]*?<Check width=\{14\} height=\{14\}/,
    'the armed clear control must turn into a confirm check')
  assert.match(progressRail, /item\.clearConfirmLabel \|\| 'Confirm clear'/,
    'the confirmation label must be item-supplied with a neutral fallback')
  assert.match(chatView, /actionLabel: resumeState\.pending \? 'Resuming…' : 'Resume'/,
    'a paused Goal should expose the one-tap resume action')
  assert.match(chatView, /actionIcon: <Play width=\{13\} height=\{13\}/,
    'the paused Goal action should spend only icon-sized visual space')
  assert.match(chatView, /ownerActionRequired: goalPresentation\?\.wait_kind === 'owner_question'/,
    'the server-owned Goal question must own the Goal wait state')
  assert.match(chatView, /monitoring: goalPresentation\?\.wait_kind === 'monitor'/,
    'the server-owned Goal wait must own the automatic monitoring state')
  assert.match(chatView, /actionableGoalPresentation\?\.status === 'paused'[\s\S]{0,80}&& !goalWaitState\.monitoring/,
    'a monitored Goal must not expose a competing manual Resume action')
  assert.match(chatView, /actionKind: 'owner-question'[\s\S]*?actionLabel: 'View question'/,
    'an owner-required Goal should expose the existing question surface')
  assert.match(chatView, /revealPendingQuestion\(pendingQuestionEl\)/,
    'the Goal question action must reveal the real pending question card')
  assert.match(chatView, /const ariaStatus = goalWaitState\.ownerActionRequired && goalAriaStatus/,
    'screen readers must hear the owner handoff before generic turn activity')
  assert.match(chatView, /onActionItem=\{handleGoalRailAction\}/,
    'the rail must route Resume and owner-question actions through their owner')
  assert.match(progressRail, /className="chat__progress-action"/,
    'the shared rail must render item-supplied actions without encoding Goal semantics')
  assert.match(progressRail, /item\.actionIcon \|\| item\.actionLabel/,
    'an accessible action label may render as a compact supplied icon')
  assert.match(
    chatCss,
    /\.chat__progress-action\s*\{[\s\S]*?color: var\(--muted\)/,
    'the paused Goal action should stay visually neutral',
  )
  assert.match(
    chatCss,
    /\.chat__progress-step--completed\s*\{\s*color: var\(--muted\)/,
    'completed Goal status should stay visually neutral',
  )
  assert.doesNotMatch(progressRail, /chat__progress-toggle-mark/,
    'the whole goal label should expand naturally without a disclosure arrow')
  assert.match(
    chatView,
    /handleResumeGoal[\s\S]{0,180}handleResume\(\)/,
    'goal and recovery-card Resume share the acknowledged lifecycle action, not a composer send',
  )
})

test('a /goal draft renders the composer goal chip', () => {
  assert.match(chatView, /const draftGoal = draftGoalObjective\(input\)/,
    'ChatView must derive the draft objective from the live composer text')
  assert.match(chatView, /draftGoal !== null && <GoalDraftChip objective=\{draftGoal\} \/>/,
    'the draft chip must render only for an actual /goal draft')
  assert.match(chatCss, /\.chat__goal-draft\s*\{/,
    'the draft chip needs its frosted-card styling')
  assert.match(chatCss, /\.chat__progress-clear\s*\{/,
    'the clear X needs its button styling')
})
