import test from 'node:test'
import assert from 'node:assert/strict'
import { carrierMessages, projectPeerTimeline, peerRecordTool, peerTime } from '../peerTimeline.js'
const note = (id, ts, extras = {}) => ({ id, created_at: ts, sender_chat_id: 'peer', recipient_chat_id: 'chat', sender_name: 'Other agent', body: 'A decision-changing note', kind: 'finding', ...extras })
const carrier = (notes, extras = {}) => ({ role: 'user', hidden: true, kind: 'peer_message', ts: 2000, steered: true, content: `<agent_coordination>\n${JSON.stringify({ messages: notes })}\n</agent_coordination>`, ...extras })

test('only typed hidden carriers can become incoming mail, never owner text', () => {
  const msg = carrier([note('one', 1500)])
  assert.equal(carrierMessages(msg).length, 1)
  assert.deepEqual(carrierMessages({ ...msg, hidden: false }), [])
  assert.deepEqual(carrierMessages({ ...msg, kind: 'continuation' }), [])
  assert.deepEqual(carrierMessages({ ...msg, content: '<agent_coordination>{bad}</agent_coordination>' }), [])
})
test('steered notes retain their exact boundary even after mailbox retention', () => {
  const input = [{ role: 'assistant', ts: 1000 }, carrier([note('one', 1500)]), { role: 'assistant', ts: 3000 }]
  const result = projectPeerTimeline(input, [], 'chat')
  assert.equal(result.slots.get(1)[0].observedDelivery, 'during_work')
  assert.equal(input[1].hidden, true)
})
test('mailbox and hidden carrier are one row; delivery evidence wins over requested delivery', () => {
  const msg = note('one', 1500, { delivery: 'next_turn' })
  const result = projectPeerTimeline([{ ts: 1000 }, carrier([msg])], [msg, msg], 'chat')
  assert.equal(result.slots.get(1).length, 1)
  assert.equal(result.slots.get(1)[0].observedDelivery, 'during_work')
})
test('quiet incoming mail appears without inventing a model read receipt', () => {
  const result = projectPeerTimeline([{ ts: 1000 }, { ts: 3000 }], [note('quiet', 2500, { delivery: 'next_turn' })], 'chat')
  assert.equal(result.slots.get(1)[0].observedDelivery, undefined)
  assert.equal(result.slots.get(1)[0].delivery, 'next_turn')
})
test('existing outgoing tool receipt gets metadata rather than a duplicate timeline row', () => {
  const sent = note('sent', 1500, { sender_chat_id: 'chat', recipient_chat_id: 'peer', recipient_name: 'Other agent' })
  const tool = { tool_use_id: 'tool', peer_message: { status: 'sent', body: sent.body, count: 1, peers: ['Other agent'] } }
  const result = projectPeerTimeline([{ ts: 1000, blocks: [tool] }, { ts: 3000 }], [sent], 'chat')
  assert.equal(result.tools.get('tool')[0].id, 'sent')
  assert.equal(result.slots.size, 0)
})
test('active tool receipts also deduplicate before the durable mirror catches up', () => {
  const sent = note('sent', 1500, { sender_chat_id: 'chat', recipient_name: 'Other agent' })
  const tool = { tool_use_id: 'live', peer_message: { status: 'sent', body: sent.body, count: 1 } }
  const result = projectPeerTimeline([{ ts: 1000 }], [sent], 'chat', [tool])
  assert.equal(result.tools.get('live')[0].id, 'sent')
  assert.equal(result.slots.size, 0)
})
test('same body to another recipient or in another turn is not swallowed', () => {
  const sent = note('sent', 1500, { sender_chat_id: 'chat', recipient_name: 'Different agent' })
  const tool = { tool_use_id: 'tool', peer_message: { status: 'sent', body: sent.body, peers: ['Other agent'] } }
  const result = projectPeerTimeline([{ ts: 1000, blocks: [tool] }, { ts: 3000 }], [sent], 'chat')
  assert.equal(result.tools.size, 0)
  assert.equal(result.slots.get(1).length, 1)
})
test('old unseen history stays outside the loaded transcript and malformed dates do not create rows', () => {
  const result = projectPeerTimeline([{ ts: 2000 }], [note('old', 1000), note('bad', 'bad'), note('tail', 3000)], 'chat')
  assert.deepEqual([...result.slots.values()].flat().map(m => m.id), ['tail'])
})
test('received direction and full body are preserved, including markup as plain text', () => {
  const body = '<script>not executable</script>\n' + 'x'.repeat(3900)
  const tool = peerRecordTool(note('one', 1500, { body }), 'chat')
  assert.equal(tool.peer_message.direction, 'read')
  assert.equal(tool.peer_message.notes[0].body, body)
  assert.equal(peerTime('2026-09-08T12:00:00'), Date.parse('2026-09-08T12:00:00Z'))
})

test('a retained carrier excerpt is never presented as a full message', () => {
  const tool = peerRecordTool(note('old', 1500, { truncated: true }), 'chat')
  assert.equal(tool.peer_message.notes[0].body_truncated, true)
})

test('incoming notes join surrounding tools without modifying saved conversation', async () => {
  const { foldPeerActivity } = await import('../peerTimeline.js')
  const messages = [{ role: 'assistant', ts: 1000, blocks: [{ type: 'tool', tool: 'Bash' }] }, { role: 'assistant', ts: 3000, blocks: [{ type: 'text', content: 'Answer' }] }]
  const projection = foldPeerActivity(messages, projectPeerTimeline(messages, [note('incoming', 2000)], 'chat'), 'chat')
  assert.equal(projection.slots.size, 0)
  assert.equal(projection.messages[0].blocks.length, 2)
  assert.equal(messages[0].blocks.length, 1)
  assert.equal(projection.tools.get('peer-incoming')[0].id, 'incoming')
})
test('prose remains a boundary for grouped incoming notes', async () => {
  const { foldPeerActivity } = await import('../peerTimeline.js')
  const messages = [{ role: 'assistant', ts: 1000, blocks: [{ type: 'text', content: 'Before' }] }, { role: 'user', ts: 3000 }]
  assert.equal(foldPeerActivity(messages, projectPeerTimeline(messages, [note('incoming', 2000)], 'chat'), 'chat').slots.size, 1)
})

test('consecutive hidden deliveries keep their order when joining the same tool stretch', async () => {
  const { foldPeerActivity } = await import('../peerTimeline.js')
  const first = note('first', 2000), second = note('second', 2100)
  const messages = [{ role: 'assistant', ts: 1000, blocks: [{ type: 'text', content: 'Intro' }] }, carrier([first]), carrier([second], { ts: 2100 }), { role: 'assistant', ts: 3000, blocks: [{ type: 'tool', tool: 'Bash' }] }]
  const folded = foldPeerActivity(messages, projectPeerTimeline(messages, [first, second], 'chat'), 'chat')
  assert.deepEqual(folded.messages[3].blocks.slice(0, 2).map(block => block.tool_use_id), ['peer-first', 'peer-second'])
})

test('a suppressed active mirror cannot swallow incoming activity', async () => {
  const { foldPeerActivity } = await import('../peerTimeline.js')
  const messages = [{ role: 'assistant', ts: 1000, blocks: [{ type: 'tool', tool: 'Bash' }] }]
  const folded = foldPeerActivity(messages, projectPeerTimeline(messages, [note('live', 2000)], 'chat'), 'chat', 0)
  assert.equal(folded.slots.get(1)[0].id, 'live')
  assert.equal(folded.messages[0].blocks.length, 1)
})
