import { test } from 'node:test'
import assert from 'node:assert/strict'
import { trayHelpers } from '../goalHelpers.js'
import { isTerminalHelperStatus } from '../workingHelper.js'

import {
  HANDOFF_ROW_LIMIT,
  handoffGroups,
  handoffSummaryLabel,
  helperState,
  shouldSummarizeHandoffs,
} from '../handoffSummary.js'

const helper = (id, status) => ({ delegation_id: id, task_key: id, provider: 'claude', status })
const wait = (id, extra = {}) => ({ id, kind: 'command', description: id, ...extra })
const ownerWait = id => wait(id, { delivery_pending: true, resume_blocker: 'manual_resume' })

test('a few handoffs show as rows; one more, or a capped list, folds into a summary', () => {
  assert.equal(HANDOFF_ROW_LIMIT, 3)
  assert.equal(shouldSummarizeHandoffs({ helpers: [helper('a', 'running'), helper('b', 'running')], waits: [wait('w')] }), false)
  assert.equal(shouldSummarizeHandoffs({ helpers: [helper('a', 'running'), helper('b', 'running')], waits: [wait('w'), wait('x')] }), true)
  assert.equal(shouldSummarizeHandoffs({ helpers: [helper('a', 'running')], unlistedHelpers: 4 }), true)
  assert.equal(shouldSummarizeHandoffs({}), false)
})

test('the summary counts calmly by state, owner first, and never names a helper', () => {
  const helpers = [
    ...Array.from({ length: 12 }, (_, i) => helper(`h${i}`, 'running')),
    helper('ask-1', 'needs_input'), helper('ask-2', 'needs_input'),
    helper('held', 'paused'), helper('done', 'completed'), helper('broke', 'failed'),
  ]
  const label = handoffSummaryLabel({ helpers, waits: [ownerWait('o'), wait('a'), wait('b'), wait('c')], unlistedHelpers: 2 })
  assert.equal(label, '1 needs you · 12 helpers working · 1 on hold · 2 waiting on coordinator · 3 waits · 1 finished · 1 failed · 2 more helpers')
  assert.equal(handoffSummaryLabel({ helpers: [helper('a', 'running')], waits: [wait('w')] }), '1 helper working · 1 wait')
  assert.doesNotMatch(label, /h0|ask-1|held/)
})

test('expanded rows group by who acts next: needs you, working, waiting, finished', () => {
  const groups = handoffGroups({
    helpers: [helper('done', 'completed'), helper('ask', 'needs_input'), helper('run', 'running'), helper('held', 'paused')],
    waits: [wait('check'), ownerWait('owner')],
  })
  assert.deepEqual(groups.map(group => [group.key, group.count]), [['needs-you', 1], ['working', 2], ['waiting', 2], ['finished', 1]])
  assert.deepEqual(groups[0].waits.map(item => item.id), ['owner'])
  assert.deepEqual(groups[1].helpers.map(item => item.delegation_id), ['run', 'held'])
  assert.deepEqual([groups[2].helpers.map(item => item.delegation_id), groups[2].waits.map(item => item.id)], [['ask'], ['check']])
  // Empty groups are left out.
  assert.deepEqual(handoffGroups({ helpers: [helper('run', 'running')] }).map(group => group.key), ['working'])
  assert.equal(helperState(helper('x', 'interrupted')), 'interrupted')
})

test('the tray selects exact bounded union totals only for the matching visible Goal', () => {
  const items = Array.from({ length: 16 }, (_, i) => ({ id: `bg-${i}`, status: 'running' }))
  for (const count of [20, 40]) {
    const projection = { count: 20, items, with_goal: { goal_id: 'goal', count, items } }
    const { rows, unlistedHelpers } = trayHelpers(projection, { id: 'goal' })
    assert.equal(rows.length, 16)
    assert.equal(unlistedHelpers, count - 16)
    assert.equal(handoffSummaryLabel({ helpers: rows, unlistedHelpers }),
      `16 helpers working · ${count - 16} more helpers`)
    assert.equal(trayHelpers(projection, null).unlistedHelpers, 4)
    assert.equal(trayHelpers(projection, { id: 'different' }).unlistedHelpers, 4)
  }
})

test('failed, interrupted and review-needed helpers retain their distinct outcomes', () => {
  const helpers = ['failed', 'interrupted', 'needs_review'].map(status => helper(status, status))
  assert.equal(handoffSummaryLabel({ helpers }), '1 failed · 1 needs review · 1 interrupted')
  assert.equal(handoffGroups({ helpers })[0].count, 3)
})


test('preview terminality does not confuse an unfinished hold with a stopped helper', () => {
  for (const status of ['accepted', 'retrying', 'starting', 'running', 'resuming', 'paused', 'needs_input', 'waiting']) {
    assert.equal(isTerminalHelperStatus(status), false, status)
  }
  for (const status of ['completed', 'done', 'failed', 'needs_review', 'interrupted', 'stopped', 'cancelled', 'killed']) {
    assert.equal(isTerminalHelperStatus(status), true, status)
  }
})
