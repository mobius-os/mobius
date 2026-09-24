import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readChatContinuityPage } from '../chatSummaryHistory.js'

test('reads one bounded journal page and requests the legacy baseline without full snapshot copies', async () => {
  let requested
  const page = { title: 'A chat', summary: 'Current handoff', entries: [{ revision: 1, digest: 'first' }], next_after_revision: 1, has_more: true }
  const result = await readChatContinuityPage('chat id', 0, async path => {
    requested = path
    return { ok: true, json: async () => page }
  })

  assert.match(requested, /after_revision=0/)
  assert.match(requested, /limit=20/)
  assert.match(requested, /include_legacy=true/)
  assert.doesNotMatch(requested, /full=true/)
  assert.equal(result.summary, 'Current handoff')
  assert.equal(result.entries[0].digest, 'first')
  assert.equal(result.has_more, true)
})

test('empty and legacy-only pages remain valid', async () => {
  const legacy = await readChatContinuityPage('legacy', 0, async () => ({
    ok: true,
    json: async () => ({
      title: 'Legacy chat', summary: 'Current baseline',
      entries: [{ revision: 0, legacy_baseline: true, legacy_markdown: 'raw legacy note' }],
      next_after_revision: 0, has_more: false,
    }),
  }))
  assert.equal(legacy.entries[0].legacy_markdown, 'raw legacy note')
  const empty = await readChatContinuityPage('empty', 0, async () => ({
    ok: true, json: async () => ({ entries: [], next_after_revision: 0, has_more: false }),
  }))
  assert.deepEqual(empty.entries, [])
})
