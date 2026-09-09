/* Recorded activity positions survive Q&A continuation and text growth. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { insertPositionedActivity } from '../activityPosition.js'
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
