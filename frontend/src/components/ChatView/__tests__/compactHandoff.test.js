import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'

const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const { default: WaitingCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingCard.jsx')
const { default: ProgressRail } = await vite.ssrLoadModule('/src/components/ChatView/ProgressRail.jsx')
const { default: GoalHistoryCard } = await vite.ssrLoadModule('/src/components/ChatView/GoalHistoryCard.jsx')
const { WaitCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
const goal = { id: 'goal-a', revision: 4, objective: 'Verify release', status: 'paused', resumable: true, pause_reason: 'deferred', hold_reason: 'Paid verification was deferred.', handoff: { kind: 'none' } }

test('a held Goal uses the existing expandable panel with one continuation action', () => {
  const handoff = goalContinuationHandoff(goal)
  const html = render(h(ProgressRail, { items: [{ key: 'goal', label: 'Goal · On hold · 1/2',
    expandable: true, details: h('p', null, handoff.description), actionLabel: handoff.actionLabel,
  }], onActionItem: () => {} }))
  assert.match(html, /chat__progress-rail/)
  assert.match(html, /Goal · On hold/)
  assert.match(html, /Continue this work/)
  assert.match(html, /aria-expanded="false"/)
  assert.match(html, /chat__panel-chevron/)
  assert.doesNotMatch(html, /chat__handoff|role="alert"/)
  assert.equal(goalContinuationHandoff({ ...goal, resumable: false }), null)
})

test('details explain action scope without changing the action', () => {
  const html = render(h(WaitingCard, { ariaLabel: 'Waiting details', expanded: true,
    rows: [{ label: 'Scope', value: 'Continuing does not approve a declined action.' }],
    action: { label: 'Continue this work', onClick: () => {} },
  }))
  assert.match(html, /aria-expanded="true"/)
  assert.match(html, /Continuing does not approve/)
  assert.match(html, /Continue this work/)
})

test('Goal panel preserves disabled actions and shows exact continuation failures', () => {
  const item = { key: 'goal', label: 'Goal · On hold', expandable: true,
    actionLabel: 'Continue this work', actionDisabled: true, actionError: 'This Goal changed.' }
  const html = render(h(ProgressRail, { items: [item], onActionItem: () => {} }))
  assert.match(html, /disabled=""/)
  assert.match(html, /role="alert"/)
  assert.match(html, /This Goal changed/)
})

test('completed outcome remains a transcript receipt after leaving the current progress rail', () => {
  const html = render(h(GoalHistoryCard, { summary: { id: 'goal-a', objective: 'Verify release', status: 'completed', task_totals: { completed: 3 }, result: { summary: 'Verified in staging.' } } }))
  assert.match(html, /Verify release/)
  assert.match(html, /Completed/)
  assert.doesNotMatch(html, /Continue this work/)
})


test('all finished Goals are read-only records with an expandable plan', () => {
  for (const status of ['completed', 'cannot_complete', 'cancelled']) {
    const summary = { id: 'goal-a', objective: 'Verify release', status,
      plan: { tasks: [{ id: 'check', title: 'Check release', status: 'completed' }] } }
    const html = render(h(GoalHistoryCard, { summary }))
    assert.match(html, /Verify release/)
    assert.match(html, /chat__goal-history-summary/)
    assert.match(html, /chat__panel-chevron/)
    assert.doesNotMatch(html, /View details/)
    assert.match(html, /Check release/)
    assert.doesNotMatch(html, /<button|Clear|Abandon|Continue this work/)
  }
})

test('completed Goals show the checklist without repeating the final reply', () => {
  for (const result of ['The release is ready.', { summary: 'The release is ready.' }]) {
    const summary = {
      objective: 'Prepare the release', status: 'completed', result,
      plan: { tasks: [{ id: 'prepare', title: 'Check the release', status: 'completed' }] },
    }
    const original = structuredClone(summary)
    const html = render(h(GoalHistoryCard, { summary }))
    assert.match(html, /Prepare the release/)
    assert.match(html, /<summary class="chat__goal-history-summary"/)
    assert.doesNotMatch(html, /View details/)
    assert.match(html, /Check the release/)
    assert.doesNotMatch(html, /The release is ready|chat__goal-result|<button/)
    assert.deepEqual(summary, original)
  }
})

test('a completed Goal with only a result has no empty disclosure or clipped objective', () => {
  const html = render(h(GoalHistoryCard, { summary: {
    objective: 'Prepare the release', status: 'completed', result: 'The release is ready.',
  } }))
  assert.match(html, /Prepare the release/)
  assert.match(html, /Completed/)
  assert.doesNotMatch(html, /The release is ready|<details|objective--preview/)
})

test('cancellation and unsuccessful Goal reasons remain visible without opening details', () => {
  for (const status of ['cannot_complete', 'cancelled', 'failed']) {
    const html = render(h(GoalHistoryCard, { summary: {
      objective: 'Prepare the release', status, result: { reason: 'The required account is unavailable.' },
      plan: { tasks: [{ id: 'prepare', title: 'Prepare', status: 'cancelled' }] },
    } }))
    const [visible, details] = html.split('</summary>')
    assert.match(visible, /The required account is unavailable\./)
    assert.doesNotMatch(details, /The required account is unavailable\./)
  }
})

test('legacy receipt without a result or plan keeps the complete objective accessible', () => {
  const html = render(h(GoalHistoryCard, { summary: { objective: 'Prepare the release', status: 'completed' } }))
  assert.match(html, /Prepare the release/)
  assert.doesNotMatch(html, /objective--preview|<details/)
})

test('collapsed cancellable Wait exposes Stop while blocked delivery reveals its existing recovery', () => {
  const wait = { id: 'wait-a', kind: 'timer', description: 'Deployment ready' }
  const props = { expanded: false, onToggle: () => {}, onCancel: () => {}, onRevealRecovery: () => {} }
  const html = render(h(WaitCard, { ...props, wait }))
  assert.match(html, /Stop waiting/)
  assert.doesNotMatch(html, /<dl/)
  for (const resume_blocker of ['manual_resume', 'resume_failed', 'restart']) {
    const blocked = render(h(WaitCard, { ...props, wait: { ...wait, delivery_pending: true, resume_blocker } }))
    assert.match(blocked, /View recovery/)
    assert.doesNotMatch(blocked, /Stop waiting/)
  }
  const activation = render(h(WaitCard, { ...props, wait: { ...wait, kind: 'platform_activation' } }))
  assert.doesNotMatch(activation, /Stop waiting/)
})

test('helper, condition and resource handoffs all use the established bordered Waiting card', async () => {
  const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
  for (const props of [
    { backgroundHelpers: { count: 1, items: [{ task_key: 'review' }] } },
    { waits: [{ id: 'timer', kind: 'timer', description: 'Deployment ready' }] },
    { resourcePause: { pause: { kind: 'memory' } }, handoff: { kind: 'automatic', reason: 'memory' } },
    { resourcePause: { pause: { kind: 'model_capacity' } }, handoff: { kind: 'recovery' }, onRevealRecovery: () => {} },
  ]) {
    const html = render(h(WaitingChip, props))
    assert.match(html, /class="chat__wait-card"/)
    assert.match(html, /class="chat__wait-summary"/)
    assert.match(html, /aria-expanded="false"/)
    assert.doesNotMatch(html, /class="chat__handoff|Next move and details/)
  }
  const helper = render(h(WaitingChip, {
    backgroundHelpers: { count: 1, items: [] },
    handoff: { kind: 'automatic', reason: 'helpers' },
  }))
  assert.match(helper, /Waiting on 1 helper/)
  assert.doesNotMatch(helper, /Waiting · Waiting/)
  assert.match(helper, /aria-label="Expand helper waiting details: Waiting on 1 helper — resumes automatically"/)
  const unowned = render(h(WaitingChip, { backgroundHelpers: { count: 1, items: [] } }))
  assert.doesNotMatch(unowned, /resumes automatically/)
})

test('stranded helper follow-up renders manual recovery without a waiting promise', async () => {
  const { default: WaitingChip, StrandedFollowupCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
  let viewed = false
  StrandedFollowupCard({ expanded: false, onToggle: () => {}, onView: () => { viewed = true } }).props.action.onClick()
  assert.equal(viewed, true)
  const html = render(h(WaitingChip, {
    chatId: 'parent-chat',
    backgroundHelpers: { count: 0, items: [] },
    handoff: { kind: 'recovery', reason: 'stranded_helper_followup', helper_id: 'failed-helper' },
    strandedFollowup: { helper_id: 'failed-helper' },
  }))
  assert.match(html, /Helper follow-up needs review/)
  assert.match(html, /will not resume automatically/)
  assert.match(html, /Needs you/)
  assert.match(html, /<button[^>]*>View helper<\/button>/)
  assert.doesNotMatch(html, /Resume chat|Continue this work/)
  assert.doesNotMatch(html, /Waiting on|resumes automatically/)

  const mixed = render(h(WaitingChip, {
    chatId: 'parent-chat',
    backgroundHelpers: { count: 1, items: [{ title: 'Other active work' }] },
    waits: [{ id: 'timer', kind: 'timer', description: 'External check' }],
    handoff: { kind: 'automatic' },
    strandedFollowup: { helper_id: 'failed-helper' },
  }))
  assert.match(mixed, /Helper follow-up needs review/)
  assert.match(mixed, /Waiting on 1 helper/)
  assert.match(mixed, /External check/)
  assert.match(mixed, /View helper/)
})

test('Waiting panel preserves expanded evidence, action errors and disabled state', () => {
  const html = render(h(WaitingCard, { expanded: true, text: 'Deployment ready', meta: 'checks every minute',
    ariaLabel: 'handoff details', onToggle: () => {}, rows: [{ label: 'Handled by', value: 'Deployment service' }],
    action: { label: 'Stop waiting', onClick: () => {}, disabled: true, error: 'Please try again.' },
  }))
  assert.match(html, /chat__wait-card--expanded/)
  assert.match(html, /aria-expanded="true"/)
  assert.match(html, /Deployment service/)
  assert.match(html, /disabled=""/)
  assert.match(html, /role="alert"/)
  assert.match(html, /Please try again/)
  assert.match(html, /<\/button>[\s\S]*<button[^>]+class="chat__wait-cancel"/)
})

test('settled waits remain read-only transcript records after the active Waiting block is gone', async () => {
  const { default: WaitHistoryCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitHistoryCard.jsx')
  for (const [status, label] of [['met', 'Wait completed'], ['expired', 'Wait reached its deadline'], ['failed', 'Wait check failed'], ['cancelled', 'Wait stopped']]) {
    const html = render(h(WaitHistoryCard, { summary: { id: 'wait-a', description: 'Release checks finish', status, duration_seconds: 125, checks_count: 3 } }))
    assert.match(html, /chat__wait-history/)
    assert.ok(html.includes(label))
    assert.match(html, /Release checks finish/)
    assert.match(html, /3 checks · 2m 5s/)
    assert.doesNotMatch(html, /<button|Stop waiting|View recovery/)
  }
})
