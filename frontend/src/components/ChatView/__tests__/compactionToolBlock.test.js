import assert from 'node:assert/strict'
import test from 'node:test'

import { compactionBrief } from '../compactionToolBlock.js'

// The reframe (feedback item E) replaced the generic CompactChat tool block
// with CompactionCard. The load-bearing contract these tests guard is that
// the portable briefing TEXT still resolves from a stored compaction message
// — the same text chat.py's `_latest_compaction_brief` replays into the next
// provider. compactionBrief is what CompactionCard renders, so asserting its
// output keeps the briefing visible across the reframe.

test('compactionBrief reads the plain-text content', () => {
  const brief = compactionBrief({
    role: 'assistant',
    kind: 'compaction',
    content: 'Goal: keep context. Next: continue.',
  })

  assert.equal(brief, 'Goal: keep context. Next: continue.')
})

test('compactionBrief returns empty string when there is nothing to show', () => {
  assert.equal(compactionBrief({ role: 'assistant', kind: 'compaction' }), '')
  assert.equal(compactionBrief({ kind: 'compaction', content: '   ' }), '')
})
