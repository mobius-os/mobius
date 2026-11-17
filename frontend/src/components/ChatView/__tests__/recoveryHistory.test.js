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
  // The detail read and stream promotion stamp the paused run's end on its row.
  const rows = [{ ...pause('restart'), ts: 1, run_ended_ts: 100 }, resume()]
  const saved = structuredClone(rows)
  const grouped = display(rows)
  assert.equal(grouped[0].hidden, true)
  assert.deepEqual(grouped[1].recovery_events.map(e => [e.type, e.ts]), [['pause', 100], ['resume', 200]])
  assert.equal(grouped[1].recovery_events[0].source.pause.kind, 'restart')
  assert.deepEqual(rows, saved)
  assert.equal(display([{ ...pause(), ts: 1 }, resume()])[1].recovery_events[0].ts, undefined,
    'an answer start is not a pause timestamp')
})

test('a recompute over the same rows reuses the combined resume row', () => {
  const rows = [pause(), resume()]
  assert.equal(display(rows)[1], display(rows)[1])
})

test('peer notes and helper results between pause and resume stay between them', () => {
  const note = [{ id: 'n', type: 'peer_message' }]
  const carrier = { role: 'user', hidden: true, kind: 'delegation_result', blocks: [] }
  for (const [rows, timeline] of [
    // Arrived during the pause: drawn just before the resume row.
    [[pause(), resume()], { slots: new Map([[1, note]]) }],
    // Positioned on the pause row itself, which must stay drawn.
    [[{ ...pause(), id: 'paused' }, resume()], { positions: new Map([['paused', note]]) }],
    // Drawn on a hidden carrier row between them.
    [[pause(), carrier, resume()], { slots: new Map([[1, note]]) }],
  ]) {
    const grouped = combineRecoveryHistory(supersedeResumedPauseBlocks(rows), timeline)
    assert.notEqual(grouped[0].hidden, true)
    assert.equal(grouped[0].blocks.at(-1).type, 'pause_record')
    assert.equal(grouped.at(-1).recovery_events, undefined)
  }
  const grouped = combineRecoveryHistory(supersedeResumedPauseBlocks([pause(), carrier, resume()]), {})
  assert.equal(grouped[0].hidden, true, 'an empty hidden carrier is no boundary')
})

test('a pause-only row never falls back to its legacy content', () => {
  const grouped = display([{ ...pause(), content: 'Paused for a platform update' }, resume()])
  assert.equal(grouped[0].hidden, true)
})

test('a resumed answer that paused at once keeps its own notice without recorded history', () => {
  const resumed = { ...pause(), continuation_reason: 'goal_handoff', ts: 150 }
  const grouped = display([{ role: 'user', content: 'Go' }, resumed, resume()])
  assert.notEqual(grouped[1].hidden, true)
  assert.equal(grouped[2].recovery_events, undefined)
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
