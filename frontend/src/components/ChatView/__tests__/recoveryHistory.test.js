/* Recovery history is grouped without rewriting events or crossing conversation. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { combineRecoveryHistory, supersedeResumedPauseBlocks } from '../chatRuntimeState.js'

const pause = (kind = 'usage_limit', extra = {}) => ({ role: 'assistant', blocks: [
  { type: 'error', resumable: true, pause: { kind }, ...extra },
] })
const resume = (reason = 'manual', ts = 200) => ({ role: 'assistant', continuation_reason: reason, ts, blocks: [] })
const display = rows => combineRecoveryHistory(supersedeResumedPauseBlocks(rows))

test('settled pause and resume become one notice, preserving cause and truthful times', () => {
  const rows = [pause('restart', { ts: 100 }), resume()]
  rows[0].ts = 1
  const saved = structuredClone(rows)
  const grouped = display(rows)
  assert.equal(grouped[0].hidden, true)
  assert.deepEqual(grouped[1].recovery_events.map(e => [e.type, e.ts]), [['pause', 100], ['resume', 200]])
  assert.equal(grouped[1].recovery_events[0].source.pause.kind, 'restart')
  assert.deepEqual(rows, saved)
  assert.equal(display([pause(), resume()])[1].recovery_events[0].ts, undefined,
    'an answer start is not a pause timestamp')
})

test('a Goal and prose before the pause stay in place while the pause joins its resume', () => {
  const row = pause()
  row.blocks.unshift({ type: 'text', content: 'Done' }, { type: 'goal_history', summary: { id: 'goal' } })
  const grouped = display([row, resume()])
  assert.equal(grouped[0].hidden, undefined)
  assert.deepEqual(grouped[0].blocks, row.blocks.slice(0, -1))
  assert.equal(grouped[1].recovery_events.length, 2)
})

test('consecutive distinct interruptions preserve every deliberate transition in one group', () => {
  const retry = { ...resume(), blocks: pause('restart').blocks }
  const grouped = display([pause(), retry, resume('restart', 300)])
  assert.equal(grouped[0].hidden, true)
  assert.equal(grouped[1].hidden, true)
  assert.deepEqual(grouped[2].recovery_events.map(e => e.type), ['pause', 'resume', 'pause', 'resume'])
  assert.deepEqual(grouped[2].recovery_events.filter(e => e.type === 'resume').map(e => e.reason), ['manual', 'restart'])
})

test('conversation, tool work, questions and outcomes prevent unrelated history from merging', () => {
  for (const block of [{ type: 'text', content: 'Work' }, { type: 'tool', name: 'read' }, { type: 'question' }, { type: 'goal_history' }]) {
    const retry = { ...resume(), blocks: [block, ...pause('restart').blocks] }
    const grouped = display([pause(), retry, resume('restart')])
    assert.notEqual(grouped[1].hidden, true)
    assert.equal(grouped[1].recovery_events.length, 2)
    assert.equal(grouped[2].recovery_events.length, 2)
  }
  const rows = [pause(), { role: 'user', content: 'Other request' }, resume()]
  assert.equal(display(rows)[2].recovery_events, undefined)
})

test('unresolved pauses keep their recovery controls and untouched identity', () => {
  const rows = [pause()]
  assert.equal(display(rows), rows)
})
