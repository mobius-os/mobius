import { isRetiredAppEntry, retireAppEntries } from '../navHistory.js'
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  MAX_STANDALONE_HISTORY_ENTRIES,
  pushStandaloneHistoryEntry,
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

test('standalone uses shell retirement and skips dead document levels without replaying them', () => {
  const registry = new Map([
    ['old', { appId: '81', status: 'live' }],
    ['other', { appId: '82', status: 'live' }],
  ])
  assert.deepEqual(retireAppEntries(registry, 81), ['old'])
  assert.deepEqual(retireAppEntries(registry, 81), [])
  assert.equal(isRetiredAppEntry(registry.get('old')), true)
  assert.equal(isRetiredAppEntry(registry.get('unknown')), true)
  assert.equal(isRetiredAppEntry(registry.get('other')), false)
  registry.set('restored', { appId: '81', status: 'live' })
  const result = reconcileStandaloneHistory(
    [entry('old'), entry('restored')],
    standaloneHistoryState({}, [entry('old')]),
    { registry },
  )
  assert.deepEqual(result.commands, [{ direction: 'back', requestId: 'restored' }])
  assert.equal(result.skipRetired, true)
  const skipped = reconcileStandaloneHistory(result.entries, standaloneHistoryState({}, []), { registry })
  assert.deepEqual(skipped.commands, [])
  assert.equal(skipped.skipRetired, false)
})

function sessionHistory() {
  const states = [standaloneHistoryState({}, [])]
  let index = 0
  let childEntries = 0
  return {
    get state() { return states[index] },
    get depth() { return states.length + childEntries },
    pushState(state) { states.splice(++index, states.length, state) },
    replaceState(state) { states[index] = state },
    pushIframeEntry() { childEntries += 1 },
    back() { if (index > 0) index -= 1 },
  }
}

for (const reset of ['frame reload', 'host reload']) {
  test(`standalone ${reset} reuses only the physically current retired sentinel`, () => {
    const history = sessionHistory()
    let registry = new Map()
    let entries = pushStandaloneHistoryEntry(history, [], registry, 81, entry('old'))
    if (reset === 'host reload') {
      registry = new Map()
      entries = readStandaloneHistoryEntries(history.state)
    } else {
      retireAppEntries(registry, 81)
    }
    entries = pushStandaloneHistoryEntry(history, entries, registry, 81, entry('restored'))
    assert.equal(history.depth, 2, 'restoration adds no ghost Back level')
    assert.deepEqual(entries, [entry('restored')])
    history.back()
    const result = reconcileStandaloneHistory(entries, history.state, { registry })
    assert.deepEqual(result.commands, [{ direction: 'back', requestId: 'restored' }])
    assert.equal(result.skipRetired, false)
  })

  test(`standalone ${reset} child history leaves the host retired slot reusable`, () => {
    const history = sessionHistory()
    let registry = new Map()
    let entries = pushStandaloneHistoryEntry(history, [], registry, 81, entry('old'))
    if (reset === 'host reload') registry = new Map()
    else retireAppEntries(registry, 81)
    const state = history.state
    history.pushIframeEntry()
    assert.equal(history.state, state, 'child pushState leaves top-level state unchanged')
    entries = pushStandaloneHistoryEntry(history, entries, registry, 81, entry('restored'))
    assert.equal(history.depth, 3, 'only child history grew')
    assert.deepEqual(entries, [entry('restored')])
  })
}

test('standalone never reuses a retired entry whose identity cannot be proven', () => {
  const history = sessionHistory()
  const registry = new Map()
  let entries = pushStandaloneHistoryEntry(history, [], registry, 81)
  retireAppEntries(registry, 81)
  entries = pushStandaloneHistoryEntry(history, entries, registry, 81, entry('restored'))
  assert.equal(history.depth, 3)
  assert.equal(entries.length, 2)
})

test('a rejected standalone history write cannot register a live owner', () => {
  const registry = new Map()
  const history = { state: null, pushState() { throw new Error('blocked') } }
  assert.equal(pushStandaloneHistoryEntry(history, [], registry, 81, entry('new')), null)
  assert.equal(registry.size, 0)
})

test('anonymous standalone requests have distinct ownership keys and a new branch prunes Forward owners', () => {
  const history = sessionHistory()
  const registry = new Map()
  let entries = pushStandaloneHistoryEntry(history, [], registry, 81)
  entries = pushStandaloneHistoryEntry(history, entries, registry, 81)
  assert.equal(registry.size, 2)
  assert.notEqual(entries[0].ownershipId, entries[1].ownershipId)
  assert.deepEqual(reconcileStandaloneHistory(entries, standaloneHistoryState({}, []), { registry }).commands,
    [{ direction: 'back', requestId: null }, { direction: 'back', requestId: null }])
  entries = pushStandaloneHistoryEntry(history, entries, registry, 81, entry('old'))
  history.back()
  entries = readStandaloneHistoryEntries(history.state)
  entries = pushStandaloneHistoryEntry(history, entries, registry, 81, entry('new'))
  assert.equal(registry.has('old'), false)
  assert.equal(registry.has('new'), true)
  assert.equal(entries.length, 3)
})
