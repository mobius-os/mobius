/* Recorded activity positions survive Q&A continuation and text growth. */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  insertPositionedActivity,
  mergeAdjacentCompactActivityEntries,
  mergeAdjacentPeerActivityEntries,
  mergePositionedActivityEntries,
} from '../activityPosition.js'
const note = (id, block_index, text_offset) => ({ id, created_at: 1000, body: id, sender_chat_id: 'other', display_position: { assistant_message_id: 'answer', block_index, ...(text_offset === undefined ? {} : { text_offset }) } })
const entries = blocks => blocks.map((item, idx) => ({ item, idx }))
test('Q&A continuation keeps the note between answered question and new response', () => {
  const blocks = [{ type: 'question', question_id: 'q', answers: { answer: 'yes' } }, { type: 'text', content: 'Response keeps growing' }]
  const output = insertPositionedActivity(entries(blocks), [note('one', 1)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), ['question', 'tool', 'text'])
  assert.equal(output[2].idx, 1)
  assert.equal(blocks.length, 2)
})
test('recorded text offset stays fixed as more text arrives', () => {
  const blocks = [{ type: 'text', content: 'Before.\n\nAfter.' }]
  const output = insertPositionedActivity(entries(blocks), [note('one', 0, 9)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.content || e.item.tool_use_id), ['Before.\n\n', 'peer-one', 'After.'])
})
test('same boundary notes retain event order and do not renumber existing tools', () => {
  const blocks = [{ type: 'tool', tool_use_id: 'original' }]
  const output = insertPositionedActivity(entries(blocks), [note('a', 0), note('b', 0)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.tool_use_id), ['peer-a', 'peer-b', 'original'])
  assert.equal(output[2].idx, 0)
})
test('an anchor ahead of a delayed stream snapshot remains visible exactly once', () => {
  const output = insertPositionedActivity([], [note('a', 3)], [], 'chat')
  assert.deepEqual(output.map(e => e.item.tool_use_id), ['peer-a'])
})

test('mid-paragraph activity never cuts markdown syntax in half', () => {
  const blocks = [{ type: 'text', content: 'A **bold phrase** still growing' }]
  const output = insertPositionedActivity(entries(blocks), [note('a', 0, 8)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.content || e.item.tool_use_id), ['peer-a', blocks[0].content])
})

test('stable question reference survives live absorption of its saved tool twin', () => {
  const blocks = [{ type: 'question', question_id: 'q', answers: { yes: true } }, { type: 'text', content: 'Following answer' }]
  const event = note('a', 2)
  event.display_position.block_key = 'question:q'
  event.display_position.block_distance = 1
  const output = insertPositionedActivity(entries(blocks), [event], [], 'chat')
  assert.deepEqual(output.map(e => e.item.type), ['question', 'tool', 'text'])
})

test('an anchor nested in compact activity stays before later prose and owner card', () => {
  const blocks = [
    {
      type: 'activity', entries: [{
        idx: 72, item: { type: 'tool', tool: 'Bash', tool_use_id: 'send-peer' },
      }],
    },
    { type: 'text', content: 'Final explanation.' },
    { type: 'question', question_id: 'restart', questions: [] },
  ]
  const event = note('a', 73)
  event.display_position.block_key = 'tool:send-peer'
  event.display_position.block_distance = 1
  const output = insertPositionedActivity(entries(blocks), [event], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), [
    'activity', 'tool', 'text', 'question',
  ])
})

test('a peer row adjacent to compact activity joins its high-level summary', () => {
  const activity = {
    type: 'activity', start: 0, end: 4,
    entries: [{ idx: 0, item: { type: 'tool', tool: 'Bash', tool_use_id: 'command' } }],
  }
  const peer = {
    item: { type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-note' },
    idx: 'peer-note',
  }
  const output = mergeAdjacentPeerActivityEntries([
    { item: activity, idx: 0 }, peer,
  ])
  assert.equal(output.length, 1)
  assert.equal(output[0].item.type, 'activity')
  assert.equal(output[0].item.positioned_entries[0].item.tool_use_id, 'peer-note')
  assert.equal(output[0].item.positioned_entries[0].positionIndex, 4)
})

test('adjacent compact fragments become one disclosure with every detail range', () => {
  const first = {
    type: 'activity', activity_id: 'first', message_index: 4, start: 0, end: 8,
    tool_count: 3,
    entries: [{ idx: 0, item: { type: 'tool', tool: 'Bash', tool_use_id: 'command' } }],
  }
  const peer = {
    type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-note', status: 'done',
  }
  const second = {
    type: 'activity', activity_id: 'second', message_index: 6, start: 0, end: 5,
    tool_count: 2,
    entries: [{ idx: 0, item: { type: 'tool', tool: 'Edit', tool_use_id: 'edit' } }],
  }
  const output = mergeAdjacentCompactActivityEntries(entries([first, peer, second]))
  assert.equal(output.length, 1)
  assert.equal(output[0].item.detail_segments.length, 3)
  assert.equal(output[0].item.tool_count, 6)
  assert.deepEqual(
    output[0].item.entries.map(entry => entry.item.tool),
    ['Bash', 'PeerMessage', 'Edit'],
  )
})

test('helper completions do not split adjacent compact activity fragments', () => {
  const first = {
    type: 'activity', activity_id: 'first', message_index: 4, start: 0, end: 4,
    tool_count: 1,
    entries: [{ idx: 0, item: { type: 'tool', tool: 'Bash', tool_use_id: 'command' } }],
  }
  const helper = {
    type: 'helper_result', id: 'helper', activityId: 'helper', status: 'completed',
  }
  const second = {
    type: 'activity', activity_id: 'second', message_index: 6, start: 0, end: 3,
    tool_count: 1,
    entries: [{ idx: 0, item: { type: 'tool', tool: 'Edit', tool_use_id: 'edit' } }],
  }

  const output = mergeAdjacentCompactActivityEntries(entries([first, helper, second]))
  assert.equal(output.length, 1)
  assert.equal(output[0].item.tool_count, 3)
  assert.deepEqual(
    output[0].item.entries.map(entry => entry.item.type),
    ['tool', 'helper_result', 'tool'],
  )
})

test('a nested compact anchor preserves a later recorded boundary', () => {
  const blocks = [
    {
      type: 'activity', entries: [{
        idx: 72, item: { type: 'tool', tool: 'Bash', tool_use_id: 'send-peer' },
      }],
    },
    { type: 'text', content: 'First later block.' },
    { type: 'text', content: 'Second later block.' },
  ]
  const event = note('a', 74)
  event.display_position.block_key = 'tool:send-peer'
  event.display_position.block_distance = 2
  const output = insertPositionedActivity(entries(blocks), [event], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), [
    'activity', 'text', 'tool', 'text',
  ])
})

test('a nested non-positive anchor stays before its compact activity', () => {
  const blocks = [{
    type: 'activity', entries: [{
      idx: 2, item: { type: 'thinking', thinking_id: 'thought' },
    }],
  }]
  const event = note('a', 2)
  event.display_position.block_key = 'thinking:thought'
  event.display_position.block_distance = 0
  const output = insertPositionedActivity(entries(blocks), [event], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), ['tool', 'activity'])
})
test('steer replay offsets use the projected text coordinate without moving the event', () => {
  const blocks = [{ type: 'text', content: 'Before.\n\nAfter.', source_text_offset: 100 }]
  const output = insertPositionedActivity(entries(blocks), [note('a', 0, 109)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.content || e.item.tool_use_id), ['Before.\n\n', 'peer-a', 'After.'])
})

test('a busy-parent helper result keeps its own disclosure at the recorded frontier', () => {
  const blocks = [{ type: 'text', content: 'Before.\n\nAfter.' }]
  const event = {
    id: 'delegation:helper:completed', activityId: 'delegation:helper:completed',
    type: 'helper_result', created_at: 1000, status: 'completed', body: 'Done',
    display_position: { assistant_message_id: 'answer', block_index: 0, text_offset: 9 },
  }
  const output = insertPositionedActivity(entries(blocks), [event], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.content || e.item.type), [
    'Before.\n\n', 'helper_result', 'After.',
  ])
  assert.equal(output.filter(entry => entry.item === event).length, 1)
})

test('peer notes inside a compact raw range stay inside that activity', () => {
  const blocks = [{
    type: 'activity', start: 10, end: 20,
    entries: [
      { idx: 10, item: { type: 'tool', tool: 'Bash', tool_use_id: 'a' } },
      { idx: 19, item: { type: 'tool', tool: 'Edit', tool_use_id: 'b' } },
    ],
  }]
  const output = insertPositionedActivity(entries(blocks), [note('inside', 15)], blocks, 'chat')
  assert.equal(output.length, 1)
  assert.equal(output[0].item.type, 'activity')
  assert.equal(output[0].item.positioned_entries[0].item.tool_use_id, 'peer-inside')
  assert.deepEqual(
    mergePositionedActivityEntries(
      [{ idx: 10, item: { type: 'tool', tool: 'Bash' } }, { idx: 19, item: { type: 'tool', tool: 'Edit' } }],
      output[0].item.positioned_entries,
    ).map(entry => entry.idx),
    [10, 'peer-inside', 19],
  )
})

const helperAt = (block_index, extra = {}) => ({
  id: 'delegation:review:completed', activityId: 'delegation:review:completed',
  type: 'helper_result', status: 'completed', created_at: 1000,
  display_position: { assistant_message_id: 'answer', block_index, ...extra },
})

test('a helper result inside a sampled compact run stays at its step, not the response tail', () => {
  // Compaction samples repeated tools, so the recorded anchor (the 40th
  // command) is absent from the projection. The stored range still owns it.
  const blocks = [
    { type: 'activity', start: 2, end: 60, entries: [
      { idx: 2, item: { type: 'tool', tool: 'Bash', tool_use_id: 'first' } },
      { idx: 3, item: { type: 'tool', tool: 'Bash', tool_use_id: 'second' } },
    ] },
    { type: 'text', content: 'Later findings.', raw_index: 60 },
    { type: 'activity', start: 61, end: 90, entries: [
      { idx: 61, item: { type: 'tool', tool: 'Edit', tool_use_id: 'edit' } },
    ] },
  ]
  const helper = helperAt(41, { block_key: 'tool:fortieth', block_distance: 1 })
  const output = insertPositionedActivity(entries(blocks), [helper], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), ['activity', 'text', 'activity'])
  assert.equal(output[0].item.positioned_entries[0].item, helper)
  assert.equal(output[0].item.positioned_entries[0].positionIndex, 41)
})

test('a compact run edge joins that activity, while later prose splits at its offset', () => {
  const blocks = [
    { type: 'activity', start: 0, end: 5, entries: [
      { idx: 0, item: { type: 'tool', tool: 'Bash', tool_use_id: 'first' } },
    ] },
    { type: 'text', content: 'Before.\n\nAfter.', raw_index: 5 },
    { type: 'activity', start: 6, end: 9, entries: [
      { idx: 6, item: { type: 'tool', tool: 'Edit', tool_use_id: 'edit' } },
    ] },
  ]
  const atEdge = insertPositionedActivity(entries(blocks), [note('edge', 5)], blocks, 'chat')
  assert.equal(atEdge.length, 3)
  assert.equal(atEdge[0].item.positioned_entries[0].item.tool_use_id, 'peer-edge')

  const inProse = insertPositionedActivity(entries(blocks), [helperAt(5, {
    text_offset: 9, block_key: 'thinking:sampled-out', block_distance: 1,
  })], blocks, 'chat')
  assert.deepEqual(inProse.map(e => e.item.content || e.item.type), [
    'activity', 'Before.\n\n', 'helper_result', 'After.', 'activity',
  ])
})

test('stored indices survive an omitted question twin in the compact projection', () => {
  // Stored: 0 text, 1 twin tool (omitted), 2 question, 3 text.
  const blocks = [
    { type: 'text', content: 'Asked.', raw_index: 0 },
    { type: 'question', question_id: 'q', questions: [], raw_index: 2 },
    { type: 'text', content: 'Answered.', raw_index: 3 },
  ]
  const output = insertPositionedActivity(entries(blocks), [note('late', 3)], blocks, 'chat')
  assert.deepEqual(output.map(e => e.item.type), ['text', 'question', 'tool', 'text'])
})
