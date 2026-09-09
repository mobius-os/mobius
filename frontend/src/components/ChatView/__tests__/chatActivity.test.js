import test from 'node:test'
import assert from 'node:assert/strict'
import { projectChatActivity } from '../chatActivity.js'
const helper = (id, created_at, extra = {}) => ({ id: `delegation:${id}:completed`, type: 'helper_result', created_at, delegation_id: id, body: 'Result', consumption: 'available', ...extra })
const peer = (id, created_at, extra = {}) => ({ id: `peer:${id}`, type: 'peer_message', created_at, sender_chat_id: 'other', body: 'Note', ...extra })
const rows = result => [...result.slots.values()].flat()

test('helper replay is one event and never changes conversation or consumes the result', () => {
  const messages = Object.freeze([Object.freeze({ ts: 1000 }), Object.freeze({ ts: 3000 })])
  const event = Object.freeze(helper('one', 2000))
  const result = projectChatActivity(messages, [event, event], 'chat')
  assert.equal(rows(result).length, 1)
  assert.equal(result.slots.get(1)[0].consumption, 'available')
  assert.equal(event.consumption, 'available')
  assert.equal(messages.length, 2)
})
test('peer envelopes deduplicate with retained mailbox carriers', () => {
  const event = peer('one', 2000)
  const carrier = { ts: 2100, hidden: true, kind: 'peer_message', content: `<agent_coordination>${JSON.stringify({ messages: [{ ...event, id: 'one' }] })}</agent_coordination>` }
  const result = projectChatActivity([{ ts: 1000 }, carrier], [event], 'chat')
  assert.equal(rows(result).length, 1)
  assert.equal(rows(result)[0].activityId, 'peer:one')
})
test('mixed activity has deterministic timestamp ties independent of page order', () => {
  const events = [peer('two', 2000), helper('one', 2000)]
  assert.deepEqual(rows(projectChatActivity([{ ts: 1000 }], events, 'chat')), rows(projectChatActivity([{ ts: 1000 }], events.toReversed(), 'chat')))
})
test('older unloaded and invalid activity does not get moved into the current window', () => {
  assert.deepEqual(rows(projectChatActivity([{ ts: 2000 }], [helper('old', 1000), helper('invalid', 'invalid')], 'chat')), [])
})
test('empty transcript still shows retained activity; updated consumption replaces replay', () => {
  const event = helper('one', 2000)
  const result = projectChatActivity([], [event, { ...event, consumption: 'incorporated' }], 'chat')
  assert.equal(result.slots.get(0).length, 1)
  assert.equal(result.slots.get(0)[0].consumption, 'incorporated')
})

test('neutral helper consumption states survive activity projection unchanged', () => {
  for (const consumption of ['unknown', 'notified']) {
    const event = helper(consumption, 2000, { consumption })
    const result = projectChatActivity([], [event], 'chat')
    assert.equal(result.slots.get(0)[0].consumption, consumption)
  }
})

test('stopped and owner-waiting transcript tails do not hide terminal helper results', () => {
  const tails = [
    { role: 'assistant', ts: 2000, blocks: [{ type: 'error', message: 'Stopped' }] },
    { role: 'assistant', ts: 2000, blocks: [{ type: 'question', question_id: 'owner-choice' }] },
  ]
  for (const tail of tails) {
    const result = projectChatActivity([{ role: 'user', ts: 1000 }, tail], [helper('terminal', 3000)], 'chat')
    assert.deepEqual(rows(result).map(event => event.id), ['delegation:terminal:completed'])
  }
})

test('incorporated helper replay stays one activity beside its hidden delivery carrier', () => {
  const available = helper('incorporated', 2000)
  const carrier = {
    role: 'user', ts: 2500, hidden: true, kind: 'delegation_result',
    content: '<delegation_results>durable provider context</delegation_results>',
  }
  const result = projectChatActivity(
    [{ role: 'user', ts: 1000 }, carrier, { role: 'assistant', ts: 3000 }],
    [available, { ...available, consumption: 'incorporated' }],
    'chat',
  )
  assert.equal(rows(result).length, 1)
  assert.equal(rows(result)[0].consumption, 'incorporated')
})

test('recorded peer and helper positions remain visible without a duplicate slot row', () => {
  const position = { assistant_message_id: 'active-answer', block_index: 0, text_offset: 4 }
  const result = projectChatActivity(
    [{ id: 'active-answer', role: 'assistant', ts: 1000 }],
    [peer('positioned', 2000, { display_position: position }), helper('busy-parent', 3000, { display_position: position })],
    'chat',
  )
  // Peer position behavior remains owned by the existing projection.
  assert.deepEqual(rows(result), [])
  assert.deepEqual(
    result.positions.get('active-answer').map(event => event.activityId),
    ['peer:positioned', 'delegation:busy-parent:completed'],
  )
})
