import assert from 'node:assert/strict'
import test from 'node:test'

import { compactionBrief } from '../compactionToolBlock.js'

// The reframe (feedback item E) replaced the generic CompactChat tool block
// with CompactionCard. The load-bearing contract these tests guard is that
// the portable briefing TEXT still resolves from a stored compaction message
// — the same text chat.py's `_latest_compaction_brief` replays into the next
// provider. compactionBrief is what CompactionCard renders, so asserting its
// output keeps the briefing visible across the reframe and across legacy
// message shapes.

test('compactionBrief reads the plain-text content first', () => {
  const brief = compactionBrief({
    role: 'assistant',
    kind: 'compaction',
    content: 'Goal: keep context. Next: continue.',
    blocks: [{ type: 'text', content: 'fallback summary' }],
  })

  assert.equal(brief, 'Goal: keep context. Next: continue.')
})

test('compactionBrief falls back to a legacy CompactChat tool block output', () => {
  const brief = compactionBrief({
    role: 'assistant',
    kind: 'compaction',
    blocks: [{
      type: 'tool',
      tool: 'CompactChat',
      input: 'POST /api/chats/chat-123/compact',
      output: 'stored summary from the old tool block',
      status: 'done',
      defaultOpen: false,
    }],
  })

  assert.equal(brief, 'stored summary from the old tool block')
})

test('compactionBrief falls back to a legacy text block when content is absent', () => {
  const brief = compactionBrief({
    role: 'assistant',
    kind: 'compaction',
    blocks: [{ type: 'text', content: 'Summary from the old block.' }],
  })

  assert.equal(brief, 'Summary from the old block.')
})

test('compactionBrief returns empty string when there is nothing to show', () => {
  assert.equal(compactionBrief({ role: 'assistant', kind: 'compaction' }), '')
  assert.equal(compactionBrief({ kind: 'compaction', content: '   ' }), '')
})
