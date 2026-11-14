import { test } from 'node:test'
import assert from 'node:assert/strict'
import { trayHelpers } from '../goalHelpers.js'

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
  assert.equal(shouldSummarizeHandoffs({ helpers: [helper('a', 'running')], cappedBackgroundCount: 4 }), true)
  assert.equal(shouldSummarizeHandoffs({}), false)
})

test('the summary counts calmly by state, owner first, and never names a helper', () => {
  const helpers = [
    ...Array.from({ length: 12 }, (_, i) => helper(`h${i}`, 'running')),
    helper('ask-1', 'needs_input'), helper('ask-2', 'needs_input'),
    helper('held', 'paused'), helper('done', 'completed'), helper('broke', 'failed'),
  ]
  const label = handoffSummaryLabel({ helpers, waits: [ownerWait('o'), wait('a'), wait('b'), wait('c')], cappedBackgroundCount: 2 })
  assert.equal(label, '1 needs you · 12 helpers working · 1 on hold · 2 waiting on coordinator · 3 waits · 1 finished · 1 stopped · Background list capped (2 total)')
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
  assert.equal(helperState(helper('x', 'interrupted')), 'stopped')
})

// Source totals cannot identify membership in a union with uncapped Goal rows.
// In particular, subtracting all Goal rows would hide disjoint background work.
for (const overlap of [true, false]) {
  test(`capped background and ${overlap ? 'overlapping' : 'disjoint'} Goal populations preserve rows without inventing an unseen union`, () => {
    const background = Array.from({ length: 20 }, (_, i) => ({ id: `bg-${i}`, status: 'running' }))
    const goal = { plan: { delegations: overlap ? background :
      Array.from({ length: 20 }, (_, i) => ({ id: `goal-${i}`, status: 'running' })) } }
    const { rows, cappedBackgroundCount } = trayHelpers({ count: 20, items: background.slice(0, 16) }, goal)
    assert.equal(rows.length, overlap ? 20 : 36)
    assert.equal(new Set(rows.map(row => row.delegation_id)).size, rows.length)
    assert.equal(cappedBackgroundCount, 20)
    assert.equal(handoffSummaryLabel({ helpers: rows, cappedBackgroundCount }),
      `${rows.length} helpers working · Background list capped (20 total)`)
    assert.doesNotMatch(handoffSummaryLabel({ helpers: rows, cappedBackgroundCount }), /more helpers/)
  })
}
