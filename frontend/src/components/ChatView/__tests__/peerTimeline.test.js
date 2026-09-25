import test from 'node:test'
import assert from 'node:assert/strict'
import {
  carrierMessages,
  foldAssistantActivityFragments,
  mergeProjectedPeerActivity,
  peerRecordTool,
  peerTime,
  projectPeerTimeline,
} from '../peerTimeline.js'
import { insertPositionedActivity } from '../activityPosition.js'
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

test('an active mirror absorbs incoming activity so it shares the live tool stretch', async () => {
  const { foldPeerActivity } = await import('../peerTimeline.js')
  const messages = [{ role: 'assistant', ts: 1000, blocks: [{ type: 'tool', tool: 'Bash' }] }]
  const folded = foldPeerActivity(messages, projectPeerTimeline(messages, [note('live', 2000)], 'chat'), 'chat')
  assert.equal(folded.slots.size, 0)
  assert.equal(folded.messages[0].blocks.length, 2)
  assert.equal(folded.messages[0].blocks[1].tool_use_id, 'peer-live')
})

test('projected peer rows join a newer live payload once without replacing it', () => {
  const bash = { type: 'tool', tool: 'Bash', tool_use_id: 'bash' }
  const later = { type: 'thinking', thinking_id: 'later' }
  const peer = { type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-note' }
  assert.deepEqual(
    mergeProjectedPeerActivity([bash, later], [peer, bash], [bash]),
    [peer, bash, later],
  )
  assert.deepEqual(
    mergeProjectedPeerActivity([peer, bash, later], [peer, bash], [bash]),
    [peer, bash, later],
  )
})

test('saved fragments of one assistant turn share an uninterrupted activity tail', () => {
  const activity = id => ({ type: 'activity', activity_id: id, entries: [] })
  const messages = [
    { id: 'run', role: 'assistant', blocks: [{ type: 'text', content: 'Intro' }, activity('one')] },
    { role: 'user', hidden: true, blocks: [] },
    { id: 'run:assistant:1', role: 'assistant', blocks: [
      { type: 'text', content: '' }, activity('two'),
    ] },
    { role: 'user', hidden: true, blocks: [] },
    { id: 'run:assistant:2', role: 'assistant', blocks: [activity('three'), { type: 'error', content: 'Paused' }] },
  ]
  const positions = new Map([['run:assistant:2', [{
    id: 'inside-third',
    display_position: { assistant_message_id: 'run:assistant:2', block_index: 0 },
  }]]])
  const folded = foldAssistantActivityFragments(messages, new Map(), -1, positions)
  const output = folded.messages
  assert.deepEqual(output[0].blocks.map(block => block.activity_id || block.type), [
    'text', 'one', 'two', 'three',
  ])
  assert.equal(output[2].hidden, true)
  assert.deepEqual(output[4].blocks.map(block => block.type), ['error'])
  assert.equal(folded.positions.get('run')[0].display_position.assistant_message_id, 'run')
  assert.equal(folded.positions.has('run:assistant:2'), false)
  assert.deepEqual(messages[0].blocks.map(block => block.activity_id || block.type), ['text', 'one'])
})

test('the active assistant fragment remains owned by the live surface', () => {
  const messages = [
    { id: 'run', role: 'assistant', blocks: [{ type: 'tool', tool: 'Bash' }] },
    { id: 'run:assistant:1', role: 'assistant', blocks: [{ type: 'tool', tool: 'Edit' }] },
  ]
  const { messages: output } = foldAssistantActivityFragments(messages, new Map(), 1)
  assert.equal(output[0].blocks.length, 1)
  assert.equal(output[1].hidden, undefined)
})

test('a folded fragment keeps its recorded stored coordinates', () => {
  const run = start => ({ type: 'activity', activity_id: `a${start}`, start, end: start + 4, entries: [] })
  const messages = [
    { id: 'turn', role: 'assistant', blocks: [run(0), { type: 'text', content: 'Middle.', raw_index: 4 }, run(5)] },
    { role: 'user', hidden: true, blocks: [] },
    // The later fragment restarts stored numbering at 0.
    { id: 'turn:assistant:1', role: 'assistant', blocks: [
      run(0), { type: 'text', content: 'Fragment prose.', raw_index: 4 },
    ] },
  ]
  const helper = {
    id: 'delegation:late:completed', activityId: 'delegation:late:completed',
    type: 'helper_result', status: 'completed', created_at: 1000,
    display_position: { assistant_message_id: 'turn:assistant:1', block_index: 2 },
  }
  const folded = foldAssistantActivityFragments(
    messages, new Map(), -1, new Map([['turn:assistant:1', [helper]]]),
  )
  const target = folded.messages[0]
  const [moved] = folded.positions.get('turn')
  assert.equal(moved.display_position.source_message_id, 'turn:assistant:1')
  const output = insertPositionedActivity(
    target.blocks.map((item, idx) => ({ item, idx })), [moved], target.blocks, 'chat',
  )
  // Stored index 2 of the fragment is inside the moved run — not the target's
  // own first run, which has the same stored range in a different message.
  assert.equal(output[0].item.positioned_entries, undefined)
  assert.equal(output.at(-1).item.positioned_entries[0].positionIndex, 2)
  assert.deepEqual(folded.messages[2].blocks.map(block => [block.type, block.raw_index]), [['text', 4]])
})
