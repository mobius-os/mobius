import test from 'node:test'
import assert from 'node:assert/strict'

import {
  MAX_STANDALONE_HISTORY_ENTRIES,
  readStandaloneHistoryEntries,
  reconcileStandaloneHistory,
  standaloneHistoryState,
} from '../standaloneHistory.js'

const entry = (requestId, reversible = true) => ({ requestId, reversible })

test('standalone history writes a complete stack without discarding other state', () => {
  const entries = [entry('one'), entry('two', false)]
  const state = standaloneHistoryState({ owner: 'browser' }, entries)

  assert.equal(state.owner, 'browser')
  assert.deepEqual(state.mobiusStandaloneEntries, entries)
  assert.equal(state.mobiusStandaloneDepth, 2)
  assert.deepEqual(state.mobiusStandaloneEntry, entry('two', false))
})

test('a multi-entry browser jump replays every back and forward transition in order', () => {
  const all = [entry('one'), entry('two'), entry('three')]
  const back = reconcileStandaloneHistory(
    all,
    standaloneHistoryState({}, [entry('one')]),
  )
  assert.deepEqual(back.commands, [
    { direction: 'back', requestId: 'three' },
    { direction: 'back', requestId: 'two' },
  ])

  const forward = reconcileStandaloneHistory(
    back.entries,
    standaloneHistoryState({}, all),
  )
  assert.deepEqual(forward.commands, [
    { direction: 'forward', requestId: 'two' },
    { direction: 'forward', requestId: 'three' },
  ])
})

test('an app-initiated pop suppresses exactly its own back command', () => {
  const result = reconcileStandaloneHistory(
    [entry('one'), entry('two'), entry('three')],
    standaloneHistoryState({}, [entry('one')]),
    { localPopPending: true },
  )

  assert.equal(result.consumedLocalPop, true)
  assert.deepEqual(result.commands, [
    { direction: 'back', requestId: 'two' },
  ])
})

test('legacy depth entries are reconciled from the known stack and bounded', () => {
  const current = [entry('one'), entry('two'), entry('three')]
  const legacy = readStandaloneHistoryEntries({
    mobiusStandaloneDepth: 2,
    mobiusStandaloneEntry: entry('two'),
  }, current)
  assert.deepEqual(legacy, [entry('one'), entry('two')])

  const malformed = readStandaloneHistoryEntries({
    mobiusStandaloneDepth: MAX_STANDALONE_HISTORY_ENTRIES + 100,
    mobiusStandaloneEntry: { requestId: 42, reversible: 'yes' },
  })
  assert.equal(malformed.length, MAX_STANDALONE_HISTORY_ENTRIES)
  assert.deepEqual(malformed.at(-1), { requestId: null, reversible: false })
})
