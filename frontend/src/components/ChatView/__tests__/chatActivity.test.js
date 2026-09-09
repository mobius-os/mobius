import test from 'node:test'
import assert from 'node:assert/strict'
import { projectChatActivity } from '../chatActivity.js'
const helper = (id, created_at, extra = {}) => ({ id: `delegation:${id}:completed`, type: 'helper_result', created_at, delegation_id: id, body: 'Result', consumption: 'available', ...extra })
const peer = (id, created_at) => ({ id: `peer:${id}`, type: 'peer_message', created_at, sender_chat_id: 'other', body: 'Note' })
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
