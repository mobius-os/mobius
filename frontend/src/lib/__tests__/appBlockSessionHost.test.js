import test from 'node:test'
import assert from 'node:assert/strict'
import {
  acceptsBlockState, blockEventDelivery, blockSessionEnvelope, createBlockDocuments, hostRequestAllowed,
} from '../../components/AppCanvas/appBlockSessionHost.js'

test('every frame load is a new, never-reused document generation', () => {
  const documents = createBlockDocuments()
  const first = documents.load('v1')
  const reload = documents.load('v1')
  const successor = documents.load('v2')
  assert.ok(first.generation < reload.generation && reload.generation < successor.generation)
  const remounted = createBlockDocuments().load('v1')
  assert.ok(remounted.generation > successor.generation, 'a remounted canvas never reuses a generation')
  assert.equal(documents.get('v1'), reload)
  const mount = (document, supportsAppBlocks) => ({ supportsAppBlocks, blockGeneration: document.generation })
  assert.equal(documents.mounted('v1', mount(reload, 'yes')).supported, false, 'only an explicit true is support')
  assert.equal(documents.mounted('v2', mount(successor, true)).supported, true)
  assert.equal(documents.mounted('gone', mount(successor, true)), null)
  // A same-iframe reload keeps the window: the old document's queued report
  // (or one naming no or a future generation) never sets the new support.
  for (const blockGeneration of [first.generation, undefined, null, reload.generation + 100]) {
    assert.equal(documents.mounted('v1', { supportsAppBlocks: true, blockGeneration }), null)
  }
  assert.equal(documents.get('v1').supported, false)
  documents.delete('v2')
  assert.equal(documents.get('v2'), null)
})

test('the init envelope carries the session and its document generation, nothing else', () => {
  const session = { sessionId: 's', actions: [{ key: 'a' }], retain: true, checkpoint: { id: 'c', data: 'd' }, extra: 'x' }
  assert.deepEqual(blockSessionEnvelope(session, 4), { sessionId: 's', generation: 4, actions: [{ key: 'a' }],
    checkpoint: { id: 'c', data: 'd' }, retain: true, recoveryError: null })
  assert.equal(blockSessionEnvelope(null, 4), null)
  assert.equal(blockSessionEnvelope(session, undefined), null)
})

test('state is accepted only from the live supporting document for this session', () => {
  const session = { sessionId: 's' }
  const document = { generation: 2, supported: true }
  const message = { sessionId: 's', generation: 2 }
  assert.equal(acceptsBlockState({ message, session, document, isLive: true }), true)
  for (const generation of [undefined, 1, 3, '2']) {
    assert.equal(acceptsBlockState({ message: { ...message, generation }, session, document, isLive: true }), false,
      `generation ${generation} is not this document`)
  }
  assert.equal(acceptsBlockState({ message, session, document, isLive: false }), false)
  assert.equal(acceptsBlockState({ message, session, document: { generation: 2, supported: null }, isLive: true }), false)
  assert.equal(acceptsBlockState({ message: { sessionId: 'other', generation: 2 }, session, document, isLive: true }), false)
  assert.equal(acceptsBlockState({ message, session: null, document, isLive: true }), false)
})

test('an owner event is posted once, only to the generation it was dispatched for', () => {
  const session = { sessionId: 's' }
  const document = { generation: 2, supported: true }
  const event = { sessionId: 's', generation: 2, key: 'send:a', event: 'confirm', nonce: 'n1', extra: 'dropped' }
  assert.deepEqual(blockEventDelivery({ session, event, document, sentNonce: null }),
    { type: 'moebius:app-block-action', sessionId: 's', generation: 2, key: 'send:a', event: 'confirm', nonce: 'n1' })
  assert.equal(blockEventDelivery({ session, event, document, sentNonce: 'n1' }), null, 'never replayed')
  assert.equal(blockEventDelivery({ session, event, document: { generation: 3, supported: true }, sentNonce: null }), null,
    'a promoted or reloaded document never receives the previous document event')
  assert.equal(blockEventDelivery({ session, event, document: { generation: 2, supported: false }, sentNonce: null }), null)
  assert.equal(blockEventDelivery({ session: { sessionId: 'other' }, event, document, sentNonce: null }), null)
})

test('a transcript or session canvas makes host requests only while visible', () => {
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: true, transcript: true }), true)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: false, transcript: true }), false)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v2', liveVersion: 'v1', visible: true, transcript: true }), false)
})

test('an ordinary shell app keeps its background host requests while another view is shown', () => {
  // For example Contribute polling chat status, or a projects list, from a cached pane.
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: false, transcript: false }), true)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v1', liveVersion: 'v1', visible: true, transcript: false }), true)
  assert.equal(hostRequestAllowed({ sourceVersion: 'v2', liveVersion: 'v1', visible: false, transcript: false }), false,
    'a buffered successor is still refused')
})
