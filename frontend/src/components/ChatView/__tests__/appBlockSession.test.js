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
