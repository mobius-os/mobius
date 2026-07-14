/**
 * Unit tests for queryClient.js `shouldPersistQueryKey` — the persist
 * allowlist that decides which TanStack Query cache entries are
 * mirrored to IndexedDB.
 *
 *   cd frontend && npm run test:lib
 *
 * The Settings view's offline-first behavior hinges on this: the
 * provider config + CLI versions (['settings']) and the connected-state
 * queries must persist so the panel paints from disk on open instead of
 * flashing an empty providers list. The short-lived setup-status query
 * (['auth','setup','status']) must NOT persist — it shares the 'auth'
 * head with the provider-status keys, so the match has to be by full
 * key, not by head segment.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/react-query'
import { indexedDB } from 'fake-indexeddb'
import { get } from 'idb-keyval'
import {
  flushPersistedQueryCache,
  shouldPersistQueryKey,
} from '../../queryClient.js'

test('top-level domains persist by head segment', () => {
  for (const head of ['chats', 'chat-messages', 'theme', 'apps']) {
    assert.equal(shouldPersistQueryKey([head]), true, `${head} should persist`)
  }
  // The head match ignores trailing segments (e.g. a chat id).
  assert.equal(shouldPersistQueryKey(['chat-messages', 'abc123']), true)
})

test('settings + provider/status queries persist by full key', () => {
  assert.equal(shouldPersistQueryKey(['settings']), true)
  assert.equal(
    shouldPersistQueryKey(['auth', 'provider', 'claude-status']),
    true,
  )
  assert.equal(
    shouldPersistQueryKey(['auth', 'providers', 'status']),
    true,
  )
})

test('short-lived auth queries do NOT persist despite sharing the head', () => {
  // setup-status shares ['auth', ...] with the persisted provider keys
  // but must not be mirrored — it is gating state, not panel content.
  assert.equal(
    shouldPersistQueryKey(['auth', 'setup', 'status']),
    false,
  )
})

test('unrelated keys do not persist', () => {
  assert.equal(shouldPersistQueryKey(['models', 'registry']), false)
  assert.equal(shouldPersistQueryKey(['app-token', 'some-app']), false)
  assert.equal(shouldPersistQueryKey(['owner', 'walkthrough']), false)
})

test('explicit reload handoff flushes the latest allowlisted chat cache', async () => {
  const previousIndexedDb = globalThis.indexedDB
  globalThis.indexedDB = indexedDB
  try {
    const client = new QueryClient()
    client.setQueryData(['chat-messages', 'chat-1'], {
      messages: [{ role: 'assistant', content: 'terminal line' }],
    })
    client.setQueryData(['models', 'registry'], { mustNotPersist: true })

    await flushPersistedQueryCache(client)
    const raw = await get('mobius-query-cache')
    const persisted = JSON.parse(raw)
    const keys = persisted.clientState.queries.map(q => q.queryKey)
    assert.deepEqual(keys, [['chat-messages', 'chat-1']])
    assert.equal(
      persisted.clientState.queries[0].state.data.messages[0].content,
      'terminal line',
    )
  } finally {
    globalThis.indexedDB = previousIndexedDb
  }
})
