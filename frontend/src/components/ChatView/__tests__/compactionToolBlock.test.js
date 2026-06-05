import assert from 'node:assert/strict'
import test from 'node:test'

import { compactionToolBlock } from '../compactionToolBlock.js'

test('compactionToolBlock renders legacy text compactions as CompactChat tool output', () => {
  const block = compactionToolBlock({
    role: 'assistant',
    kind: 'compaction',
    content: 'Goal: keep context. Next: continue.',
    blocks: [{ type: 'text', content: 'fallback summary' }],
  }, 'chat-123')

  assert.deepEqual(block, {
    type: 'tool',
    tool: 'CompactChat',
    input: 'POST /api/chats/chat-123/compact',
    output: 'Goal: keep context. Next: continue.',
    status: 'done',
    defaultOpen: true,
  })
})

test('compactionToolBlock falls back to the legacy text block when content is absent', () => {
  const block = compactionToolBlock({
    role: 'assistant',
    kind: 'compaction',
    blocks: [{ type: 'text', content: 'Summary from the old block.' }],
  }, 'chat-abc')

  assert.equal(block.output, 'Summary from the old block.')
  assert.equal(block.defaultOpen, true)
})

test('compactionToolBlock preserves stored tool blocks and forces them open', () => {
  const block = compactionToolBlock({
    role: 'assistant',
    kind: 'compaction',
    content: 'stored summary',
    blocks: [{
      type: 'tool',
      tool: 'CompactChat',
      input: 'POST /api/chats/chat-123/compact',
      output: 'stored summary',
      status: 'done',
      defaultOpen: false,
    }],
  }, 'chat-123')

  assert.deepEqual(block, {
    type: 'tool',
    tool: 'CompactChat',
    input: 'POST /api/chats/chat-123/compact',
    output: 'stored summary',
    status: 'done',
    defaultOpen: true,
  })
})
