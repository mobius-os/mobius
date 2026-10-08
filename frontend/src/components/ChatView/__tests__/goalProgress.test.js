import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { advanceChatRunSignal, chatRunSignalDelta, EMPTY_CHAT_RUN_SIGNAL } from '../../../lib/chatRunSignal.js'
import {
  canResumeGoal, goalStatusLabel, compactGoalObjective, draftGoalObjective,
  goalMessageObjectiveFromText, goalPresentationFromRuntime, goalTaskDisplayStatus,
  normalizeGoalPresentation, progressRailViewModel, visibleGoalTasks,
} from '../goalProgress.js'
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const streamConnection = readFileSync(new URL('../useStreamConnection.js', import.meta.url), 'utf8')
const progressRail = readFileSync(new URL('../ProgressRail.jsx', import.meta.url), 'utf8')
const goalPlanDetails = readFileSync(new URL('../GoalPlanDetails.jsx', import.meta.url), 'utf8')
const chatCss = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
const shellSource = readFileSync(new URL('../../Shell/Shell.jsx', import.meta.url), 'utf8')

const plan = {
  root_run_id: 'run-a', goal_id: 'goal-a', revision: 1,
  summary: { completed: 1, total: 2 },
  tasks: [{ id: 'verify', title: 'Verify the release', status: 'running' }],
  delegations: [],
}
const goal = {
  id: 'goal-a', revision: 1, objective: 'Ship the release', status: 'active',
  resumable: false, handoff: { kind: 'automatic', reason: 'helpers' }, plan,
}

test('message and composer formatting survives without lifecycle inference', () => {
  assert.equal(goalMessageObjectiveFromText('/goal Ship\nthen verify'), 'Ship\nthen verify')
  assert.equal(goalMessageObjectiveFromText('please /goal later'), '')
  assert.equal(goalMessageObjectiveFromText('/goal clear'), '')
  assert.equal(compactGoalObjective(' Ship\nthen verify '), 'Ship then verify')
  assert.doesNotMatch(chatView, /latestGoalObjective|goalPresentationAtRunStart|setGoalAtRunStart|active_goal_objective|activeGoalObjective:/)
})

test('runtime is authoritative including explicit clear and ordinary absence', () => {
  assert.deepEqual(goalPresentationFromRuntime({ running: false, goal }), goal)
  assert.equal(goalPresentationFromRuntime({ goal: null }), null)
  assert.equal(goalPresentationFromRuntime({ running: true, active_goal_objective: 'obsolete' }), null)
  assert.doesNotMatch(chatView, /goal_activated|goal_plan_updated|goal_cleared|activeGoalPlan|newestGoalPlan|planForGoal|\/goal-plan/)
})

test('same Goal reattachment keeps its plan and disclosure identity until a new snapshot arrives', () => {
  const before = goalPresentationFromRuntime({ goal })
  const after = goalPresentationFromRuntime({ goal: { ...goal, revision: 2 } })
  assert.equal(after.plan, before.plan)
  assert.equal(after.id, before.id)
  assert.deepEqual(progressRailViewModel(after, []), progressRailViewModel(before, []))
})

test('Goal A cannot lend its plan to Goal B even within the same execution root', () => {
  const bPlan = { ...plan, goal_id: 'goal-b', tasks: [{ id: 'next', title: 'New work', status: 'running' }] }
  const b = goalPresentationFromRuntime({ goal: { ...goal, id: 'goal-b', plan: bPlan } })
  assert.equal(b.plan, bPlan)
  assert.doesNotMatch(progressRailViewModel(b, [])[0].label, /Verify the release/)
  assert.match(progressRailViewModel(b, [])[0].label, /New work/)
  assert.equal(goalPresentationFromRuntime({ goal: { ...goal, id: 'goal-b', plan: null } }).plan, null)
})

test('required owner holds and recovery remain actionable, automatic and question ownership do not', () => {
  for (const kind of ['owner_hold', 'recovery', 'none']) {
    const held = { ...goal, status: 'paused', resumable: true, handoff: { kind } }
    assert.equal(canResumeGoal(held), true)
    assert.equal(goalStatusLabel(held), 'Interrupted')
    for (const conflict of [{ turnActive: true }, { hasPendingQuestion: true }, { chatHandoff: 'automatic' }]) {
      assert.equal(canResumeGoal(held, conflict), false)
    }
  }
  for (const kind of ['automatic', 'owner_input']) {
    assert.equal(canResumeGoal({ ...goal, status: 'paused', handoff: { kind } }), false)
  }
})

test('retained pause labels preserve owner, agent, and interruption provenance', () => {
  for (const [pause_reason, label] of [
    ['owner', 'Paused by you'], ['agent', 'Paused by agent'],
    ['unknown', 'Interrupted'], ['deferred', 'On hold'],
  ]) {
    assert.equal(goalStatusLabel({ ...goal, status: 'paused', pause_reason }), label)
  }
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
    [],
  )
})

test('a working helper outranks a stale completed task presentation', () => {
  const task = { id: 'audit', status: 'completed' }
  assert.equal(goalTaskDisplayStatus(task, [{ status: 'running' }]), 'running')
  assert.equal(goalTaskDisplayStatus(task, [{ status: 'paused' }]), 'running')
  assert.equal(goalTaskDisplayStatus(task, [{ status: 'completed' }]), 'completed')
  assert.equal(goalTaskDisplayStatus(task), 'completed')
})

test('a failed helper does not repaint a task another helper redid', () => {
  const task = { id: 'review', status: 'completed' }
  assert.equal(
    goalTaskDisplayStatus(task, [{ status: 'needs_review' }, { status: 'completed' }]),
    'completed',
  )
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
    /goalPresentationFromRuntime\(runtime\)/,
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
    /const ariaStatus = currentChatAnnouncement\(/,
    'screen readers should receive current work status rather than a retained terminal Goal',
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
    /helpersByTask\.set\(node\.plan_task/,
    'helpers nest under the plan task they recorded at start, not a name match',
  )
  assert.match(
    goalPlanDetails,
    /status=\{goalTaskDisplayStatus\(task, helpers\)\}/,
    'a task\'s working helpers should own the row presentation state',
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
  assert.match(chatView, /clearable:\s*!!goalPresentation\?\.id/,
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
  assert.match(
    chatView,
    /resume: handleResumeGoal, state: goalResumeState[\s\S]{0,200}goalId: goalPresentation\?\.id,[\s\S]{0,100}goalRevision: goalPresentation\?\.revision/,
    'Goal Resume targets its exact revision through the acknowledged lifecycle action',
  )
  assert.match(chatView, /item\?\.actionKind === 'resume-goal'[\s\S]*?handleResumeGoal\(\)/,
    'the familiar Goal rail routes continuation to the exact Goal Resume owner')
  assert.match(chatView, /\.\.\.\(continuationHandoff \? \{[\s\S]*?actionKind: 'resume-goal'/,
    'saved questions, recovery and live-work guards still own action availability')
  assert.doesNotMatch(chatView, /<GoalHandoff|RetainedGoalContext/,
    'no duplicate continuation card or terminal mutation context remains')
  assert.match(chatView, /actionKind: 'owner-question'[\s\S]*?actionLabel: 'View question'/,
    'an owner-required Goal should expose the existing question surface')
  assert.match(chatView, /revealPendingQuestion\(pendingQuestionEl\)/,
    'the Goal question action must reveal the real pending question card')
  assert.match(chatView, /const ariaStatus = currentChatAnnouncement\(/,
    'screen readers use the shared current-responsibility projection, tested for saved-card precedence')
  assert.match(chatView, /onActionItem=\{handleGoalRailAction\}/,
    'the rail must route the owner-question action through its owner')
  assert.match(progressRail, /className="chat__progress-action"/,
    'the shared rail must render item-supplied actions without encoding Goal semantics')
  assert.match(progressRail, /item\.actionIcon \|\| item\.actionLabel/,
    'an accessible action label may render as a compact supplied icon')
  assert.match(
    chatCss,
    /\.chat__progress-action\s*\{[\s\S]*?color: var\(--muted\)/,
    'the Goal question action should stay visually neutral',
  )
  assert.match(
    chatCss,
    /\.chat__progress-step--completed\s*\{\s*color: var\(--muted\)/,
    'completed Goal status should stay visually neutral',
  )
  assert.doesNotMatch(progressRail, /chat__progress-toggle-mark/,
    'the whole goal label should expand naturally without a disclosure arrow')
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

// Exercise the mounted view's actual signal-drain callback with its I/O owners stubbed.
function invalidationHarness({ running = false } = {}) {
  const callback = chatView.match(/const reconcileExternalActivity = useCallback\((async \(\) => \{[\s\S]*?\n  \}), \[/)[1]
  const state = {
    hiddenRef: { current: false }, activationSettledRef: { current: true },
    processedExternalSignalRef: { current: EMPTY_CHAT_RUN_SIGNAL },
    externalSignalRef: { current: EMPTY_CHAT_RUN_SIGNAL },
    externalReconcileInFlightRef: { current: false }, externalClaimedRunRef: { current: false },
    sendingRef: { current: running }, isStreamingRef: { current: running },
    localStartRequestRef: { current: null }, fetchGenRef: { current: 0 }, chatId: 'owner',
    chatRunSignalDelta, invalidateSharedRuntimeRead: () => {},
    readRuntime: async () => {}, readDetail: async () => {},
    refreshRuntimeState: (...args) => state.readRuntime(...args),
    fetchMessages: (...args) => state.readDetail(...args),
  }
  const drain = new Function('state', `const { ${Object.keys(state).join(', ')} } = state; return ${callback}`)(state)
  const handler = shellSource.split("} else if (ev.type === 'chat_wait_changed') {")[1]
    .split("} else if (ev.type === 'chat_run_started')")[0]
  const route = new Function('ev', 'markChatRunReconcile', 'refreshChatRows', handler)
  return { state, drain, invalidate(event = { type: 'chat_wait_changed', chat_id: 'owner', source: 'goal' }) {
    route(event, chatId => {
      if (chatId === state.chatId) {
        state.externalSignalRef.current = advanceChatRunSignal(state.externalSignalRef.current, 'chat_run_reconcile')
      }
    }, () => {})
  } }
}

test('idle invalidation reads runtime and its embedded plan without a turn or revision change', async () => {
  const owner = invalidationHarness()
  let snapshot = { running: false, goal: structuredClone(goal) }
  let displayed = goalPresentationFromRuntime(snapshot)
  let runtimeReads = 0
  let detailReads = 0
  owner.state.readRuntime = async () => { runtimeReads++; displayed = goalPresentationFromRuntime(snapshot) }
  owner.state.readDetail = async () => { detailReads++ }
  snapshot.goal.plan.summary.completed = 2
  snapshot.goal.plan.tasks[0].status = 'completed'
  owner.invalidate()
  await owner.drain()
  assert.equal(runtimeReads, 1)
  assert.equal(detailReads, 1)
  assert.equal(owner.state.fetchGenRef.current, 1, 'pre-invalidation reads are fenced out')
  assert.equal(displayed.plan.summary.completed, 2)
  assert.equal(displayed.plan.tasks[0].status, 'completed')
})

test('nested live invalidations arriving during a read drain again and keep the same Goal attached', async () => {
  const owner = invalidationHarness({ running: true })
  const snapshot = { running: true, goal: structuredClone(goal) }
  let reads = 0
  let displayed = goalPresentationFromRuntime(snapshot)
  snapshot.goal.plan.delegations = [{
    id: 'parent', task_key: 'internal.parent', plan_task: 'verify', title: 'Coordinate verification',
    provider: 'claude', status: 'running', question: null, children: [{
      id: 'child', task_key: 'internal.child', plan_task: 'verify', title: 'Verify the release',
      provider: 'codex', status: 'running', question: null, children: [],
    }],
  }]
  owner.state.readRuntime = async () => {
    reads++
    displayed = goalPresentationFromRuntime(structuredClone(snapshot))
    if (reads === 1) {
      snapshot.goal.plan.delegations[0].children[0].status = 'needs_input'
      snapshot.goal.plan.delegations[0].children[0].question = { id: 'q', text: 'Which release?', options: [] }
      owner.invalidate()
    }
  }
  owner.state.readDetail = async () => assert.fail('live invalidation must not replace the transcript')
  owner.invalidate()
  await owner.drain()
  assert.equal(reads, 2)
  assert.equal(displayed.id, goal.id)
  assert.equal(displayed.plan.delegations[0].children[0].question.text, 'Which release?')
})

test('Goal invalidation payload targets only the owning chat', async () => {
  const owner = invalidationHarness()
  owner.state.readRuntime = async () => assert.fail('another Goal chat cannot invalidate this view')
  owner.invalidate({ type: 'chat_wait_changed', chat_id: 'other-goal-chat', source: 'goal' })
  await owner.drain()
  assert.equal(owner.state.externalSignalRef.current.seq, 0)
})

test('parallel helpers and retries label their work once, not their execution rows', () => {
  const plan = {
    tasks: [{ id: 'check', title: 'Check result', status: 'pending' }],
    delegations: [
      { id: 'a', title: 'Helper work', plan_task: 'check', status: 'running' },
      { id: 'b', title: 'Helper work', plan_task: 'check', status: 'running' },
    ],
  }
  assert.deepEqual(visibleGoalTasks(plan).map(task => task.id), ['check'])
  assert.deepEqual(visibleGoalTasks({ tasks: [], delegations: plan.delegations }), [])
})
