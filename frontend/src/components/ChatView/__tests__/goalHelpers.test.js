import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { goalHelpers, trayHelpers, goalHelperWaitingLabel } from '../goalHelpers.js'
import { progressRailViewModel } from '../goalProgress.js'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'

const vite = await createSsrRenderServer()
after(() => vite.close())
const load = async name => (await vite.ssrLoadModule(`/src/components/ChatView/${name}.jsx`)).default
const Details = await load('GoalPlanDetails')
const Rail = await load('ProgressRail')

const child = {
  id: 'nested', task_key: 'private.internal.key', plan_task: 'check',
  title: 'Verify the migration', provider: 'codex', status: 'needs_input', children: [],
  question: { id: 'question-nested', text: 'Which release should I verify?', options: ['Stable', 'Preview'] },
}
const parent = {
  id: 'parent', task_key: 'private.parent.key', plan_task: 'coordinate',
  title: 'Coordinate the release', provider: 'claude', status: 'running', question: null, children: [child],
}
const goal = {
  id: 'goal-a', revision: 1, objective: 'Ship the release', status: 'active', resumable: false,
  handoff: { kind: 'automatic', reason: 'helpers' },
  plan: {
    goal_id: 'goal-a', root_run_id: 'run-a', revision: 1,
    summary: { completed: 0, total: 2 },
    tasks: [
      { id: 'coordinate', title: 'Coordinate the release', status: 'running' },
      { id: 'check', parent_id: 'coordinate', title: 'Verify the migration', status: 'pending' },
    ],
    delegations: [parent],
  },
}

test('a helper question is internal coordination: no owner banner, the task names its coordinating agent', () => {
  const items = progressRailViewModel(goal, []).map(item => ({ ...item, details: h(Details, { plan: goal.plan }) }))
  const collapsed = render(h(Rail, { items }))
  assert.match(collapsed, /aria-expanded="false"/)
  assert.match(collapsed, /Waiting on 1 helper · 1 waiting for coordinating agent · resumes automatically/)
  assert.doesNotMatch(collapsed, /Needs an answer|Waiting for you|Which release|Helper questions|Stable/)

  const details = render(h(Details, { plan: goal.plan }))
  const [checklist, history] = details.split('<details class="chat__goal-execution">')
  assert.match(checklist, /Verify the migration.*Waiting for coordinating agent/)
  assert.doesNotMatch(checklist, /Which release/)
  // The exact saved question stays inspectable in closed execution history.
  assert.match(history, /Waiting for coordinating agent/)
  assert.match(history, /Asked its coordinating agent/)
  assert.match(history, /Which release should I verify\?/)
  assert.match(history, /Options offered: Stable · Preview/)
  assert.doesNotMatch(details, /private\.|Codex|Claude|<input|<textarea|<button|Submit|Needs an answer/)
})

test('a saved owner card owns the headline while helper questions stay internal details', () => {
  const owner = { ...structuredClone(goal), status: 'paused', handoff: { kind: 'owner_input', reason: 'saved_card' } }
  assert.equal(goalHelperWaitingLabel(owner), null)
  const [rail] = progressRailViewModel(owner, [])
  assert.match(rail.label, /^Goal · Waiting for you · 0\/2/)
  assert.doesNotMatch(rail.label, /coordinating agent|Needs an answer/)
  assert.match(render(h(Details, { plan: owner.plan })), /Waiting for coordinating agent/)
})

test('helper questions never invent owner input or an automatic wake', () => {
  const only = structuredClone(goal)
  only.plan.delegations = [{ ...child, plan_task: 'check' }]
  assert.equal(goalHelperWaitingLabel(only), 'Helper waiting for coordinating agent · resumes automatically')
  // Delivered to an idle parent that ended without answering: no wake is arranged.
  const idle = { ...only, status: 'paused', handoff: { kind: 'none', reason: null } }
  assert.equal(goalHelperWaitingLabel(idle), 'On hold · Helper waiting for coordinating agent')
  const stopped = { ...only, status: 'paused', resumable: true, handoff: { kind: 'owner_hold', reason: 'owner' } }
  assert.equal(goalHelperWaitingLabel(stopped), 'On hold · Helper waiting for coordinating agent')
  assert.ok(goalContinuationHandoff(stopped))
  // While the coordinating agent's turn runs, the rail keeps its ordinary working headline.
  assert.equal(goalHelperWaitingLabel({ ...only, status: 'active' }, { turnActive: true }), null)
  const two = structuredClone(only)
  two.plan.delegations.push({ ...child, id: 'second' })
  assert.equal(goalHelperWaitingLabel(two), '2 helpers waiting for coordinating agent · resumes automatically')
})

test('a task with working and asking helpers stays in progress and names the pending question', () => {
  const plan = structuredClone(goal.plan)
  plan.delegations[0].children.push({ id: 'sibling', plan_task: 'check', status: 'running', children: [] })
  const checklist = render(h(Details, { plan })).split('<details')[0]
  assert.match(checklist, /In progress · helper waiting for coordinating agent/)
  assert.equal((checklist.match(/role="listitem"/g) || []).length, 2)
})

test('expanded helpers use plan titles and On hold, never task keys or provider names', () => {
  const plan = structuredClone(goal.plan)
  plan.delegations[0].children[0].status = 'paused'
  const html = render(h(Details, { plan }))
  assert.match(html, /Coordinate the release/)
  assert.match(html, /Verify the migration/)
  assert.match(html, /On hold/)
  assert.doesNotMatch(html, /private\.|Codex|Claude|Paused/)
})

test('the helper list has each helper once: background helpers, then direct Goal helpers still owed', () => {
  const background = { count: 2, items: [
    { id: 'parent', task_key: 'private.parent.key', provider: 'claude', status: 'running' },
    { id: 'goal-b-helper', task_key: 'other-goal', provider: 'codex', status: 'completed' },
  ] }
  const settled = { id: 'settled', task_key: 'done-already', status: 'completed', children: [] }
  const asking = { id: 'asking', task_key: 'needs-parent', provider: 'codex', status: 'needs_input', children: [] }
  const owned = structuredClone(goal)
  owned.plan.delegations.push(settled, asking)
  const { rows, unlisted } = trayHelpers(background, owned)
  // A Goal helper the background list already has is not repeated; a settled
  // Goal helper is history, and a nested helper belongs to its own parent.
  assert.deepEqual(rows.map(row => row.delegation_id), ['parent', 'goal-b-helper', 'asking'])
  assert.deepEqual(rows[2], { delegation_id: 'asking', task_key: 'needs-parent', provider: 'codex', status: 'needs_input' })
  assert.equal(unlisted, 0)
  assert.deepEqual(trayHelpers(null, null), { rows: [], unlisted: 0 })
  // The server caps listed items; the rest are counted, never invented.
  assert.equal(trayHelpers({ count: 20, items: background.items }, null).unlisted, 18)
})

test('helper counts never invent automatic wake ownership or optional recovery actions', () => {
  const noQuestion = structuredClone(goal)
  noQuestion.plan.delegations[0].children[0].status = 'running'
  noQuestion.plan.delegations[0].children[0].question = null
  assert.equal(goalHelperWaitingLabel(noQuestion), 'Waiting on 2 helpers · resumes automatically')
  assert.equal(goalHelperWaitingLabel(noQuestion, { turnActive: true }), null)
  const held = { ...noQuestion, status: 'paused', resumable: true, handoff: { kind: 'owner_hold' } }
  assert.equal(goalHelperWaitingLabel(held), 'On hold · Waiting on 2 helpers')
  assert.ok(goalContinuationHandoff(held))
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'automatic' } }), null)
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'owner_input' } }), null)
})


test('counted checklist contains work only; helper attempts stay in closed secondary history', () => {
  const plan = structuredClone(goal.plan)
  plan.delegations.push({ id: 'retry', title: 'Verify the migration', plan_task: 'check', status: 'failed' })
  const html = render(h(Details, { plan }))
  const [checklist, history] = html.split('<details class="chat__goal-execution">')
  assert.equal((checklist.match(/role="listitem"/g) || []).length, 2)
  assert.equal((checklist.match(/Verify the migration/g) || []).length, 1)
  assert.match(history, /Helper activity · 3/)
  assert.match(history, /not additional checklist steps/)
  assert.doesNotMatch(history, /role="listitem"|chat__goal-task-marker/)
  assert.doesNotMatch(html, /<details[^>]* open/)
})

test('an inherited nested helper affects its task without adding checklist rows', () => {
  const plan = structuredClone(goal.plan)
  plan.tasks[0].status = 'completed'
  plan.delegations[0].status = 'completed'
  plan.delegations[0].children = [{ id: 'inherited', status: 'running', children: [] }]
  const html = render(h(Details, { plan })).split('<details')[0]
  assert.match(html, /chat__goal-task--running/)
  assert.match(html, /In progress/)
  assert.equal((html.match(/role="listitem"/g) || []).length, 2)
})
