import test from 'node:test'
import assert from 'node:assert/strict'

import {
  collectChatDiffs,
  mergeChatDiffEntries,
  normalizeChatDiffEntries,
  summarizeChatDiffs,
} from '../chatDiffs.js'

const small = {
  diff: 'diff --git a/src/a.js b/src/a.js\n--- a/src/a.js\n+++ b/src/a.js\n@@ -1 +1 @@\n-old\n+new',
  truncated: false,
}

test('chat changes dedupe a live replay by stable tool id', () => {
  const entries = collectChatDiffs([
    { ts: 1, blocks: [{ type: 'tool', tool: 'Edit', tool_use_id: 'edit-1', edit_preview: small }] },
    { ts: 2, blocks: [{ type: 'tool', tool: 'Edit', tool_use_id: 'edit-1', edit_preview: small }] },
  ])
  assert.equal(entries.length, 1)
  assert.equal(entries[0].ts, 2)
  assert.deepEqual(summarizeChatDiffs(entries), {
    updateCount: 1,
    fileCount: 1,
  })
})

test('complete route data wins over a bounded live twin', () => {
  const full = normalizeChatDiffEntries([{ id: 'edit-1', preview: small }])
  const bounded = normalizeChatDiffEntries([{
    id: 'edit-1',
    preview: { ...small, truncated: true },
  }])
  assert.equal(mergeChatDiffEntries(full, bounded)[0].preview.truncated, false)
})
