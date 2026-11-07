import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { goalContinuationHandoff } from '../chatHandoffPresentation.js'
import { renderWithModels } from './modelRegistryRender.js'

const vite = await createSsrRenderServer()
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

test('an expanded command Wait offers its exact command behind a collapsed Command row', () => {
  const command = "curl -fsS https://preview.test/healthz | grep -q ok"
  const wait = { id: 'wait-c', kind: 'command', description: 'Preview responds', command, interval_secs: 300 }
  const props = { onToggle: () => {}, onCancel: () => {} }
  const collapsed = render(h(WaitCard, { ...props, expanded: false, wait }))
  assert.doesNotMatch(collapsed, /Command|chat__wait-command/)
  const expanded = render(h(WaitCard, { ...props, expanded: true, wait }))
  assert.match(expanded, /<dt>Command<\/dt><dd><div class="chat__wait-command"><button type="button" class="chat__wait-command-toggle" aria-expanded="false">Show<\/button><\/div><\/dd>/)
  // Hidden until asked for: the command text is not in the page yet.
  assert.doesNotMatch(expanded, /preview\.test|<pre/)
  for (const empty of [{ kind: 'timer' }, { kind: 'command', command: '   ' }]) {
    const html = render(h(WaitCard, { ...props, expanded: true, wait: { ...wait, ...empty, command: empty.command ?? null } }))
    assert.doesNotMatch(html, /<dt>Command/)
  }
})

test('a collapsed Wait is one line with Stop in its details, while blocked delivery keeps recovery visible', () => {
  const wait = { id: 'wait-a', kind: 'timer', description: 'Deployment ready' }
  const props = { expanded: false, onToggle: () => {}, onCancel: () => {}, onRevealRecovery: () => {} }
  const html = render(h(WaitCard, { ...props, wait }))
  // Beside the composer a wait must stay one line; Stopping is a deliberate
  // choice made from the expanded details.
  assert.doesNotMatch(html, /Stop waiting|chat__wait-action/)
  assert.doesNotMatch(html, /<dl/)
  const expanded = render(h(WaitCard, { ...props, expanded: true, wait }))
  assert.match(expanded, /<\/dl><div class="chat__wait-detail-action"><button type="button" class="chat__wait-cancel">Stop waiting<\/button>/)
  for (const resume_blocker of ['manual_resume', 'resume_failed', 'restart']) {
    const blocked = render(h(WaitCard, { ...props, wait: { ...wait, delivery_pending: true, resume_blocker } }))
    assert.match(blocked, /View recovery/)
    const blockedDetails = render(h(WaitCard, { ...props, expanded: true, wait: { ...wait, delivery_pending: true, resume_blocker } }))
    assert.doesNotMatch(blockedDetails, /Stop waiting/)
  }
  const activation = render(h(WaitCard, { ...props, expanded: true, wait: { ...wait, kind: 'platform_activation' } }))
  assert.doesNotMatch(activation, /Stop waiting/)
})

test('helper, condition and resource handoffs all use the established bordered Waiting card', async () => {
  const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
  for (const props of [
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
})

test('helpers are one openable row each, never a "Waiting on N helpers" count', async () => {
  const { default: WaitingChip } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
  const html = renderWithModels(h(WaitingChip, {
    chatId: 'parent-chat',
    helpers: [
      { delegation_id: 'del-a', task_key: 'review-copy', provider: 'claude', status: 'running' },
      { delegation_id: 'del-b', task_key: 'verify-build', provider: 'codex', status: 'needs_input' },
    ],
    handoff: { kind: 'automatic', reason: 'helpers' },
  }))
  assert.match(html, /class="chat__wait-card chat__helper-list"/)
  assert.equal((html.match(/chat__helper-row/g) || []).length, 2)
  // Status only: the projection has no live step or start time to tick from.
  assert.match(html, /aria-label="review-copy: Claude · Working\. Open its conversation"/)
  assert.match(html, /aria-label="verify-build: Codex · Waiting for coordinating agent\. Open its conversation"/)
  assert.doesNotMatch(html, /chat__subagent-elapsed|Waiting on \d helper|Usage unknown/)
  const capped = renderWithModels(h(WaitingChip, {
    chatId: 'parent-chat',
    helpers: [{ delegation_id: 'del-a', task_key: 'review-copy', provider: 'claude', status: 'running' }],
    unlistedHelpers: 3,
  }))
  assert.match(capped, /and 3 more helpers/)
  // Only a server-owned automatic handoff may promise the chat resumes.
  assert.match(html, /This chat resumes when they finish/)
  assert.doesNotMatch(capped, /resumes/)
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
