import test from 'node:test'
import assert from 'node:assert/strict'

import {
  collectChatDiffs,
  loadChatDiffEntries,
  mergeChatDiffEntries,
  normalizeChatDiffEntries,
  summarizeChatDiffs,
} from '../chatDiffs.js'
import { chatChangesOverview } from '../chatChangesLifecycle.js'

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

test('a cold long chat loads older edits from the authoritative route', async () => {
  const requests = []
  const authoritative = {
    id: 'older-than-loaded-window',
    ts: '2026-08-20T08:00:00Z',
    preview: small,
  }
  const entries = await loadChatDiffEntries('chat with history', {
    signal: 'request-signal',
    request: async (path, options) => {
      requests.push({ path, options })
      return {
        ok: true,
        status: 200,
        json: async () => ({ entries: [authoritative] }),
      }
    },
  })

  assert.deepEqual(requests, [{
    path: '/chats/chat%20with%20history/edit-diffs',
    options: { signal: 'request-signal' },
  }])
  assert.equal(entries[0].id, 'older-than-loaded-window')
  // A cold composer has no transcript entries in its mounted window. The
  // route-owned edit still survives that empty live supplement.
  const coldEntries = mergeChatDiffEntries(entries, [])
  assert.equal(coldEntries.length, 1)
  assert.deepEqual(
    chatChangesOverview(coldEntries, { records: [] }).unsortedPaths,
    ['src/a.js'],
  )
})
