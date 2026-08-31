import test from 'node:test'
import assert from 'node:assert/strict'

import {
  chatChangesPrimaryAction,
  preparedChangesPrimaryAction,
  contributionActionOutcome,
  contributionStartFailureOutcome,
  contributionWorkContext,
  finishContributionWork,
  followupContributionWork,
  prepareContributionWork,
  projectContributionWork,
  updatesContributionWork,
} from '../chatContributionIntent.js'

test('contribution startup keeps refresh, blocker, and service failure distinct', async () => {
  assert.deepEqual(await contributionActionOutcome(() => true), { kind: 'accepted' })
  assert.deepEqual(
    await contributionActionOutcome(() => ({ kind: 'refreshed' })),
    { kind: 'refreshed' },
  )
  assert.deepEqual(
    await contributionActionOutcome(() => ({ kind: 'blocked', message: 'Busy' })),
    { kind: 'blocked', message: 'Busy' },
  )
  assert.deepEqual(await contributionActionOutcome(() => false), { kind: 'unavailable' })
  assert.deepEqual(await contributionActionOutcome(() => undefined), { kind: 'unavailable' })
  assert.deepEqual(await contributionActionOutcome(null), { kind: 'unavailable' })
  assert.deepEqual(await contributionActionOutcome(() => {
    throw new Error('offline')
  }), { kind: 'unavailable' })

  assert.deepEqual(
    contributionStartFailureOutcome({ status: 409, detail: 'Old revision', actionChanged: true }),
    { kind: 'refreshed' },
  )
  assert.deepEqual(
    contributionStartFailureOutcome({ status: 409, detail: 'Another helper is active' }),
    { kind: 'blocked', message: 'Another helper is active' },
  )
  assert.deepEqual(
    contributionStartFailureOutcome({ status: 503, detail: 'internal detail' }),
    { kind: 'unavailable' },
  )
  assert.deepEqual(contributionStartFailureOutcome(), { kind: 'unavailable' })
})

test('chat preparation sends compact typed work rather than replaying a provider prompt', () => {
  assert.deepEqual(prepareContributionWork(' edits-1 '), {
    intent: 'prepare', expected_revision: 'edits-1', record_ids: [],
  })
  assert.deepEqual(finishContributionWork('flow-2'), {
    intent: 'finish', expected_revision: 'flow-2', record_ids: [],
  })
  assert.deepEqual(projectContributionWork({ id: ' /data/platform ' }, 'edits-3'), {
    intent: 'project', expected_revision: 'edits-3',
    project_root: '/data/platform', record_ids: [],
  })
  assert.deepEqual(updatesContributionWork([{ id: 'one' }, { id: 'two' }], 'records'), {
    intent: 'updates', expected_revision: 'records', record_ids: ['one', 'two'],
  })
  assert.deepEqual(followupContributionWork({ id: 'one' }, 'one:rev'), {
    intent: 'followup', expected_revision: 'one:rev', record_ids: ['one'],
  })
  const terminalWorkId = 'a'.repeat(64)
  assert.deepEqual(prepareContributionWork('edits-1', terminalWorkId), {
    intent: 'prepare', expected_revision: 'edits-1', record_ids: [],
    retry_of: terminalWorkId,
  })
  assert.deepEqual(contributionWorkContext({
    contributeAppId: 80,
    workState: 'attention',
    work: { id: terminalWorkId },
  }), { appId: 80, retryOf: terminalWorkId })
  assert.deepEqual(contributionWorkContext({
    contributeAppId: 80,
    workState: 'active',
    work: { id: terminalWorkId },
  }), { appId: 80, retryOf: '' })
  assert.deepEqual(contributionWorkContext({ contributeAppId: null }), {
    appId: null, retryOf: '',
  })
})

test('Changes exposes one context-aware primary action', () => {
  assert.equal(chatChangesPrimaryAction({ counts: { unsorted: 4 } }).label, 'Prepare to submit')
  assert.equal(chatChangesPrimaryAction({ counts: { attention: 2 } }).label, 'Resolve all')
  assert.equal(chatChangesPrimaryAction({ counts: { prepared: 2 } }).label, 'Review prepared')
  assert.equal(chatChangesPrimaryAction({ counts: { open: 2 } }).label, 'Check for updates')
  assert.equal(chatChangesPrimaryAction({ counts: { unsorted: 1, prepared: 1 } }).label, 'Prepare to submit')
  assert.equal(chatChangesPrimaryAction({ workState: 'active', counts: { unsorted: 4 } }), null)
  assert.equal(chatChangesPrimaryAction({ counts: { submitting: 1, prepared: 1 } }), null)
  assert.equal(chatChangesPrimaryAction({ counts: {} }), null)
})

test('prepared work resolves to one direct top action', () => {
  const ready = (id, action = 'pr') => ({
    id, action, status: 'prepared', quality_review_ready: true,
    review: { state: 'ready' },
  })
  const send = ready('send')
  const update = ready('update', 'pr_update')
  assert.equal(preparedChangesPrimaryAction([send], { connected: true }).label, 'Send PR')
  assert.equal(preparedChangesPrimaryAction([update], { connected: true }).label, 'Update PR')
  assert.equal(preparedChangesPrimaryAction([send, update], { connected: true }).label, 'Send all 2')
  assert.equal(preparedChangesPrimaryAction([
    { ...send, status: 'submitting' }, update,
  ], { connected: true }), null)
  assert.equal(preparedChangesPrimaryAction([
    { ...send, review: { state: 'needs_refresh' } }, update,
  ], { connected: true }).label, 'Fix and review all 2')

  const stack = {
    kind: 'stack', id: 'stack:direct', stack: { id: 'direct', total: 2 },
    records: [
      { ...ready('one'), stack: { id: 'direct', position: 1, total: 2 } },
      { ...ready('two'), stack: { id: 'direct', position: 2, total: 2 } },
    ],
  }
  assert.equal(preparedChangesPrimaryAction([stack], { connected: true }).label, 'Send stack')
})
