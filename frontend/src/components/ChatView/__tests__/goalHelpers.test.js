import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
import { goalHelpers, helpersOutsideGoal } from '../goalHelpers.js'
import { progressRailViewModel } from '../goalProgress.js'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const load = async name => (await vite.ssrLoadModule(`/src/components/ChatView/${name}.jsx`)).default
const Questions = await load('GoalHelperQuestions')
const Details = await load('GoalPlanDetails')
const Rail = await load('ProgressRail')
const Waiting = await load('WaitingChip')

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

test('a nested saved question is visible even in the collapsed owning Goal, with task title and no ancestor answer control', () => {
  const items = progressRailViewModel(goal, []).map(item => ({
    ...item, details: h(Details, { plan: goal.plan }), notice: h(Questions, { helpers: goalHelpers(goal) }),
  }))
  const html = render(h(Rail, { items }))
  assert.match(html, /aria-expanded="false"/)
  assert.match(html, /aria-label="Helper questions" tabindex="0"/)
  assert.match(html, /Verify the migration · Needs an answer/)
  assert.match(html, /Which release should I verify/)
  assert.match(html, /Stable/)
  assert.match(html, /Preview/)
  assert.match(html, /Waiting for its parent/)
  assert.doesNotMatch(html, /private\.|Codex|Claude|<input|<textarea|Submit|resumes automatically/)
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

test('only exactly Goal-owned helpers fold; unrelated Goal B work and unprojected counts stay separate', () => {
  const background = { count: 4, items: [{ id: 'parent' }, { id: 'nested' }, { id: 'goal-b-helper' }] }
  assert.deepEqual(helpersOutsideGoal(background, goal), { count: 2, items: [{ id: 'goal-b-helper' }] })
  assert.deepEqual(helpersOutsideGoal(background, null), background)
  assert.deepEqual(helpersOutsideGoal({ count: 1, items: [{ id: 'parent' }] }, goal), { count: 0, items: [] })
})

test('helper counts never invent automatic wake ownership or optional recovery actions', () => {
  const held = { ...goal, status: 'paused', resumable: true, handoff: { kind: 'owner_hold' } }
  assert.ok(goalContinuationHandoff(held))
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'automatic' } }), null)
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'owner_input' } }), null)
  const backgroundHelpers = { count: 1, items: [{ id: 'other', task_key: 'secret' }] }
  assert.doesNotMatch(render(h(Waiting, { backgroundHelpers, handoff: { kind: 'recovery' } })), /resumes automatically|secret/)
  assert.match(render(h(Waiting, { backgroundHelpers, handoff: { kind: 'automatic' } })), /resumes automatically/)
})
