import test from 'node:test'
import assert from 'node:assert/strict'
import { appBlockFromToken } from '../markdown/appBlock.js'
import {
  INCOMPLETE_CONFIRMATION, passiveAppBlockAllowed, sessionDocumentReset, sessionRetained, sessionSnapshot,
  sessionState, sessionStateUpdate,
} from '../markdown/appBlockSession.js'

const keys = new Set(['send:a', 'send:b'])
const parse = fields => sessionState({ type: 'moebius:app-block-state', sessionId: 's',
  actions: [{ key: 'send:a', label: 'Send', disabled: false }], ...fields }, 's', keys)

test('state is session and key scoped, bounded, plain text and HTTPS-only links', () => {
  assert.equal(sessionState({ type: 'moebius:app-block-state', sessionId: 'other', actions: [] }, 's', keys), null)
  assert.equal(sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: Array(25).fill({}) }, 's', keys), null)
  const state = sessionState({ type: 'moebius:app-block-state', sessionId: 's', notice: 'n'.repeat(600), summary: 's'.repeat(300), actions: [
    { key: 'send:a', label: 'L'.repeat(80), note: 'N'.repeat(600), status: 'Sending', statusTone: 'neon', links: [
      { label: 'PR', url: 'https://github.com/owner/repo/pull/1' },
      { label: 'bad', url: 'https://user:password@github.com/owner/repo/pull/2' },
      { label: 'bad', url: 'javascript:alert(1)' },
    ] },
    { key: 'send:a', label: 'Duplicate' }, { key: 'unknown', label: 'Wrong' },
  ] }, 's', keys)
  assert.equal(state.actions.length, 1)
  assert.equal(state.actions[0].label.length, 40)
  assert.equal(state.actions[0].note.length, 500)
  assert.equal(state.actions[0].statusTone, 'neutral')
  assert.deepEqual(state.actions[0].links, [{ label: 'PR', url: 'https://github.com/owner/repo/pull/1' }])
  assert.equal(state.notice.length, 500)
  assert.equal(state.summary.length, 240)
})

test('confirmation presents every app-supplied member or disables Confirm, never truncating', () => {
  const confirmation = [{ title: 'owner/repo: Fix it', facts: [{ label: 'Destination', value: 'owner/repo → main' }] },
    { title: 'Stack member 2', facts: [{ label: 'Version', value: 'abc123' }] }]
  const action = value => parse({ actions: [{ key: 'send:a', confirming: true, confirmation: value }] }).actions[0]
  assert.deepEqual(action(confirmation).confirmation, confirmation)
  assert.equal(action(confirmation).disabled, false)
  for (const value of [undefined, [], [{ title: '', facts: [] }], [{ title: 'x'.repeat(513), facts: [] }],
    [...confirmation, { title: 'Hidden', facts: [{ label: 'x', value: 42 }] }], Array.from({ length: 257 }, () => confirmation[0]),
    [{ title: 'Too many facts', facts: Array(9).fill({ label: 'a', value: 'b' }) }]]) {
    const state = action(value)
    assert.equal(state.confirmation, null)
    assert.equal(state.disabled, true, 'an incomplete presentation cannot offer Confirm')
    assert.equal(state.note, INCOMPLETE_CONFIRMATION)
  }
})

test('live badges replace saved badges; while confirming only app badges show', () => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'example', intent: 'open:a', title: 'Fix', interaction: 'inline', badges: [{ label: 'All clear', tone: 'success' }],
    action: { label: 'Send', intent: 'send:a' },
    items: [{ title: 'Row', intent: 'open:b', badges: [{ label: 'Saved', tone: 'neutral' }], action: { label: 'Send', intent: 'send:b' } }] }) })
  assert.deepEqual(sessionSnapshot(block, null).badges, [{ label: 'All clear', tone: 'success' }])
  const idle = parse({ actions: [{ key: 'send:a', badges: [{ label: 'Current', tone: 'neon' }, null] }] })
  assert.deepEqual(sessionSnapshot(block, idle).badges, [{ label: 'Current', tone: 'neutral' }])
  assert.deepEqual(sessionSnapshot(block, idle).items[0].badges, [{ label: 'Saved', tone: 'neutral' }])
  const confirming = parse({ actions: [{ key: 'send:a', confirming: true }] })
  assert.deepEqual(sessionSnapshot(block, confirming).badges, [])
  assert.deepEqual(sessionSnapshot(block, confirming).items[0].badges, [])
  const appOwned = parse({ actions: [{ key: 'send:a', confirming: true, badges: [{ label: 'Reviewed', tone: 'success' }] }] })
  assert.deepEqual(sessionSnapshot(block, appOwned).badges, [{ label: 'Reviewed', tone: 'success' }])
})

test('while confirming, the app list owns the block: its rows only, no saved facts, one action', () => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'example', intent: 'open:a', title: 'Fix', interaction: 'inline', action: { label: 'Send all', intent: 'send:a' },
    facts: [{ label: 'Target', value: 'saved by the conversation' }],
    items: [{ title: 'One', intent: 'open:1' }, { title: 'Two', intent: 'open:2', action: { label: 'Send', intent: 'send:b' } },
      { title: 'Three', intent: 'open:3' }] }) })
  const list = [{ title: 'App one', facts: [{ label: 'Project', value: 'owner/repo' }] }, { title: 'App two', facts: [] }]
  const rows = [
    { title: 'App one', facts: [{ label: 'Project', value: 'owner/repo' }], badges: [], action: null, confirmation: true },
    { title: 'App two', facts: [], badges: [], action: null, confirmation: true }]
  const all = sessionSnapshot(block, parse({ actions: [{ key: 'send:a', confirming: true, confirmation: list }] }))
  assert.deepEqual(all.items, rows)
  assert.deepEqual(all.facts, [], 'saved facts are not shown beside the app list')
  assert.deepEqual([all.action.intent, all.confirmation, all.title], ['send:a', true, 'Fix'])
  // An item's action owns the block the same way, drawn once as the block's action.
  const one = sessionSnapshot(block, parse({ actions: [{ key: 'send:b', confirming: true, confirmation: list,
    badges: [{ label: 'Reviewed', tone: 'success' }] }] }))
  assert.deepEqual(one.items, rows)
  assert.deepEqual(one.action, { label: 'Send', intent: 'send:b' })
  assert.deepEqual(one.badges, [{ label: 'Reviewed', tone: 'success' }], 'only the app\'s badges')
  // Without a complete list (incomplete or ambiguous), the saved block stays.
  const incomplete = sessionSnapshot(block, parse({ actions: [{ key: 'send:a', confirming: true }] }))
  assert.deepEqual(incomplete.items.map(item => item.title), ['One', 'Two', 'Three'])
  assert.equal(incomplete.facts.length, 1)
  assert.equal(incomplete.confirmation, undefined)
})

test('idle offscreen sessions are released; unacknowledged and unresolved owners are retained', () => {
  const idle = { actions: [{ busy: false, confirming: false }], retain: false, ackNonce: 'one' }
  assert.equal(sessionRetained(idle, null), false)
  assert.equal(sessionRetained(null, null), false)
  assert.equal(sessionRetained(idle, { nonce: 'two' }), true)
  assert.equal(sessionRetained(idle, { nonce: 'one' }), false)
  assert.equal(sessionRetained({ ...idle, retain: true }, null), true)
  assert.equal(sessionRetained({ actions: [{ busy: true }] }, null), true)
  assert.equal(sessionRetained({ actions: [{ confirming: true }] }, null), true)
  assert.equal(sessionRetained({ actions: [], checkpoint: { id: 'c', data: '' } }, null), true)
})

test('a new document discards idle controls and frozen confirmation but holds unresolved ownership', () => {
  assert.equal(sessionDocumentReset(null), null)
  assert.equal(sessionDocumentReset({ actions: [], retain: false }), null)
  assert.equal(sessionDocumentReset({ retain: true, actions: [{ confirming: true }], ackNonce: 'a' }, { event: 'activate', nonce: 'a' }), null)
  const pendingConfirm = sessionDocumentReset({ retain: false, actions: [{ confirming: true }], ackNonce: null }, { event: 'confirm', nonce: 'c' })
  assert.equal(pendingConfirm.retain, true)
  assert.equal(pendingConfirm.recoveryPending, true)
  const busy = { retain: true, ackNonce: 'sent', actions: [
    { key: 'send:a', confirming: true, confirmation: [{ title: 'Old', facts: [] }], disabled: false }, { key: 'send:b', busy: true }] }
  const reset = sessionDocumentReset(busy)
  assert.equal(reset.ackNonce, null)
  assert.ok(reset.actions.every(action => action.disabled && !action.confirming && action.confirmation === null))
  assert.equal(busy.actions[0].confirming, true, 'the previous state object is not mutated')
})

test('recovery is released only by the exact checkpoint acknowledgement', () => {
  const owner = sessionStateUpdate(null, parse({ retain: true, checkpoint: { id: 'attempt-1', data: '{"phase":"uncertain"}', extra: 'ignored' } }))
  assert.deepEqual(owner.checkpoint, { id: 'attempt-1', data: '{"phase":"uncertain"}' })
  const reset = sessionDocumentReset(owner)
  assert.equal(reset.recoveryError, null, 'a valid checkpoint is not an app error')
  const cold = sessionStateUpdate(reset, parse({ retain: false }))
  assert.equal(cold.actions[0].disabled, true)
  assert.equal(sessionRetained(cold, null), true)
  const wrong = sessionStateUpdate(cold, parse({ checkpointAck: 'other' }))
  assert.equal(wrong.actions[0].disabled, true)
  const imported = sessionStateUpdate(wrong, parse({ checkpointAck: 'attempt-1', checkpoint: { id: 'attempt-2', data: 'new' } }))
  assert.equal(imported.recoveryPending, false)
  assert.equal(imported.checkpoint.id, 'attempt-2')
  assert.equal(imported.actions[0].disabled, false)
  const settled = sessionStateUpdate(imported, parse({ checkpoint: null, checkpointAck: 'attempt-2' }))
  assert.equal(sessionRetained(settled, null), false)
})

test('a malformed replacement checkpoint fails closed without erasing prior data', () => {
  const prior = sessionStateUpdate(null, parse({ checkpoint: { id: 'one', data: 'opaque' } }))
  for (const bad of [{ id: '', data: 'x' }, { id: 'two', data: '💫'.repeat(9000) }, { id: 'two', data: 7 }, []]) {
    const held = sessionStateUpdate(prior, parse({ checkpoint: bad }))
    assert.deepEqual(held.checkpoint, prior.checkpoint)
    assert.equal(held.retain, true)
    assert.equal(held.actions[0].disabled, true)
    assert.match(held.recoveryError, /Open the app/)
  }
  const reported = sessionStateUpdate(prior, parse({ recoveryError: true }))
  assert.equal(reported.retain, true)
  assert.ok(reported.recoveryError)
})

test('passive admission needs the accepted grant and a server digest, never block input', () => {
  const app = { capability_contract: { runtime: { 'chat.blocks.passive': { version: 1 } } }, passive_block_module_digest: 'a'.repeat(64) }
  assert.equal(passiveAppBlockAllowed(app), true)
  for (const value of [null, {}, { appBlockSessions: true }, { ...app, passive_block_module_digest: null },
    { ...app, passive_block_module_digest: 'bad' }, { ...app, capability_contract: null },
    { ...app, capability_contract: { runtime: { 'chat.blocks.passive': { version: true } } } }]) {
    assert.equal(passiveAppBlockAllowed(value), false)
  }
})

test('across a reload only an unacknowledged confirm, the one event that may cause an effect, is held', () => {
  const idle = { retain: false, ackNonce: null, actions: [{ key: 'send:a', confirming: false, busy: false }] }
  for (const kind of ['activate', 'cancel']) {
    assert.equal(sessionDocumentReset(idle, { event: kind, nonce: 'n1' }), null, `${kind} is effect-free and discarded`)
  }
  const held = sessionDocumentReset(idle, { event: 'confirm', nonce: 'n1' })
  assert.equal(held.recoveryPending, true)
  assert.equal(held.retain, true)
  assert.ok(held.actions.every(action => action.disabled))
  assert.equal(sessionDocumentReset({ ...idle, ackNonce: 'n1' }, { event: 'confirm', nonce: 'n1' }), null,
    'an acknowledged confirm is the app\'s to report (busy, checkpoint) and is not held by the host')
})

// Live row state and a live title (building-apps.md "Passive block sessions").
const rowBlock = () => appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
  app: 'example', intent: 'open:all', title: 'Saved title', interaction: 'inline', action: { label: 'Send all', intent: 'send:a' },
  items: [{ title: 'Row A', intent: 'open:a' }, { title: 'Row B', intent: 'open:b' }] }) })
const intents = new Set(['open:a', 'open:b'])
const parseRows = fields => sessionState({ type: 'moebius:app-block-state', sessionId: 's',
  actions: [{ key: 'send:a', label: 'Send all' }], ...fields }, 's', keys, intents)

test('row state is keyed by listed item intents, bounded, plain text and HTTPS-only', () => {
  const state = parseRows({ title: 'T'.repeat(200), items: [
    { intent: 'open:a', status: 'S'.repeat(80), statusTone: 'neon', busy: true, note: 'N'.repeat(300), links: [
      { label: '#12', url: 'https://github.com/owner/repo/pull/12' }, { label: 'bad', url: 'javascript:alert(1)' },
      { label: 'bad', url: 'https://user:pw@github.com/x' }, { label: '#13', url: 'https://github.com/owner/repo/pull/13' },
      { label: '#14', url: 'https://github.com/owner/repo/pull/14' }] },
    { intent: 'open:a', status: 'Second report for the same row' },
    { intent: 'open:unlisted', status: 'Dropped' },
    { intent: 'open:b' },
  ] })
  assert.equal(state.title.length, 120)
  assert.deepEqual([...state.items.keys()], ['open:a'], 'unknown intents and empty rows are dropped; the first report wins')
  const row = state.items.get('open:a')
  assert.equal(row.status.length, 40)
  assert.equal(row.statusTone, 'neutral')
  assert.equal(row.busy, true)
  assert.equal(row.note.length, 200)
  // Up to two links are read; unsafe ones are dropped after that bound.
  assert.deepEqual(row.links, [{ label: '#12', url: 'https://github.com/owner/repo/pull/12' }])
  // Without the block's intents nothing is accepted.
  const unscoped = sessionState({ type: 'moebius:app-block-state', sessionId: 's', actions: [],
    items: [{ intent: 'open:a', status: 'Open' }] }, 's', keys)
  assert.equal(unscoped.items.size, 0)
  assert.equal(parseRows({ items: Array.from({ length: 13 }, () => ({ intent: 'open:a', status: 'x' })) }).items.size, 1)
  assert.equal(parseRows({ items: 'not a list' }).items.size, 0)
})

test('the live title and row state replace the saved ones, and an open confirmation hides row state', () => {
  const block = rowBlock()
  assert.equal(sessionSnapshot(block, null).title, 'Saved title')
  assert.equal(sessionSnapshot(block, null).items[0].live, null)
  const sent = parseRows({ title: '2 pull requests opened', items: [
    { intent: 'open:a', status: 'Open', statusTone: 'success', links: [{ label: '#12', url: 'https://github.com/owner/repo/pull/12' }] }] })
  const snapshot = sessionSnapshot(block, sent)
  assert.equal(snapshot.title, '2 pull requests opened')
  assert.deepEqual(snapshot.items.map(item => item.live?.status ?? null), ['Open', null])
  assert.equal(sessionSnapshot(block, parseRows({ title: '' })).title, 'Saved title', 'an empty title keeps the saved one')
  const confirming = parseRows({ title: 'Live', items: [{ intent: 'open:a', status: 'Open' }],
    actions: [{ key: 'send:a', confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }] })
  const owned = sessionSnapshot(block, confirming)
  assert.deepEqual(owned.items.map(item => [item.title, item.live ?? null]), [['Exact', null]])
  const incomplete = parseRows({ items: [{ intent: 'open:a', status: 'Open' }], actions: [{ key: 'send:a', confirming: true }] })
  assert.ok(sessionSnapshot(block, incomplete).items.every(item => item.live === null))
})

test('open is a navigation-only action and never coexists with confirming', () => {
  assert.equal(parseRows({ actions: [{ key: 'send:a', open: true, label: 'Open Example' }] }).actions[0].open, true)
  assert.equal(parseRows({ actions: [{ key: 'send:a', open: 'yes' }] }).actions[0].open, false)
  const both = parseRows({ actions: [{ key: 'send:a', open: true, confirming: true, confirmation: [{ title: 'Exact', facts: [] }] }] })
  assert.equal(both.actions[0].open, false)
  assert.equal(both.actions[0].confirming, true)
})

test('a recovery hold shows no live title, row state or open link', () => {
  const previous = sessionStateUpdate(null, parseRows({ title: 'Live', retain: true, checkpoint: { id: 'c1', data: '{}' },
    items: [{ intent: 'open:a', status: 'Sending', busy: true }], actions: [{ key: 'send:a', open: true }] }))
  const held = sessionDocumentReset(previous)
  assert.equal(held.title, '')
  assert.equal(held.items.size, 0)
  assert.equal(held.actions[0].open, false)
  const replacement = sessionStateUpdate(held, parseRows({ title: 'Too early', items: [{ intent: 'open:a', status: 'Open' }] }))
  assert.equal(replacement.recoveryPending, true)
  assert.equal(replacement.title, '')
  assert.equal(replacement.items.size, 0)
  assert.equal(sessionSnapshot(rowBlock(), replacement).title, 'Saved title')
})

test('the host advertises row-state, and a report can opt in to value-only row facts', async () => {
  const { SESSION_FEATURES } = await import('../markdown/appBlockSession.js')
  assert.deepEqual([...SESSION_FEATURES], ['row-state'])
  assert.equal(parseRows({ itemFacts: 'values' }).itemFacts, 'values')
  assert.equal(parseRows({ itemFacts: 'labels' }).itemFacts, '')
  assert.equal(sessionSnapshot(rowBlock(), parseRows({ itemFacts: 'values' })).itemFacts, 'values')
  assert.equal(parseRows({ items: [{ intent: 'open:a', status: 'Merged', statusTone: 'accent' }] }).items.get('open:a').statusTone, 'accent')
})
