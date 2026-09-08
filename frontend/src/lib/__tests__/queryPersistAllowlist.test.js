/**
 * Unit tests for queryClient.js `shouldPersistQueryKey` — the persist
 * allowlist that decides which TanStack Query cache entries are
 * mirrored to IndexedDB.
 *
 *   cd frontend && npm run test:lib
 *
 * The Settings view's offline-first behavior hinges on this: the
 * provider config + CLI versions (['settings']) and the canonical status
 * query must persist so the panel paints from disk on open instead of
 * flashing an empty providers list. The short-lived setup-status query
 * (['auth','setup','status']) must NOT persist — it shares the 'auth'
 * head with the provider-status keys, so the match has to be by full
 * key, not by head segment.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { dehydrate, QueryClient } from '@tanstack/react-query'
import { persistQueryClientRestore } from '@tanstack/react-query-persist-client'
import { indexedDB } from 'fake-indexeddb'
import { get } from 'idb-keyval'
import {
  awaitCacheFlushBeforeReload,
  compactPersistedChatDetails,
  flushPersistedQueryCache,
  persistOptions,
  restorePersistedClient,
  shouldPersistQueryKey,
} from '../../queryClient.js'

test('last-known shell state remains restorable after a long offline stretch', async () => {
  const source = new QueryClient()
  source.setQueryData(['chats'], [{ id: 'chat-1', title: 'Kept offline' }])
  const persisted = {
    buster: 'v1',
    timestamp: Date.now() - 30 * 24 * 60 * 60 * 1000,
    clientState: dehydrate(source),
  }
  let removed = false
  const restored = new QueryClient()

  await persistQueryClientRestore({
    ...persistOptions,
    queryClient: restored,
    persister: {
      restoreClient: async () => persisted,
      removeClient: async () => { removed = true },
    },
  })

  assert.equal(removed, false)
  assert.deepEqual(
    restored.getQueryData(['chats']),
    [{ id: 'chat-1', title: 'Kept offline' }],
  )
})

test('background persistence bounds inactive chat history to the cold page', () => {
  const messages = Array.from({ length: 30 }, (_, index) => ({ content: `line-${index}` }))
  const persisted = compactPersistedChatDetails({
    clientState: {
      queries: [{
        queryKey: ['chat-messages', 'chat-1'],
        state: { data: { offset: 4, total: 34, restorationWindowComplete: true, messages } },
      }],
    },
  })
  const data = persisted.clientState.queries[0].state.data
  assert.equal(data.offset, 14)
  assert.equal(data.messages.length, 20)
  assert.equal(data.messages[0].content, 'line-10')
  assert.equal(data.restorationWindowComplete, true)
})

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
    false,
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

test('explicit reload handoff preserves the complete loaded chat window', async () => {
  const previousIndexedDb = globalThis.indexedDB
  globalThis.indexedDB = indexedDB
  try {
    const client = new QueryClient()
    client.setQueryData(['chat-messages', 'chat-1'], {
      offset: 4,
      messages: Array.from({ length: 30 }, (_, index) => ({
        role: 'assistant',
        content: `line-${index}`,
      })),
    })
    client.setQueryData(['models', 'registry'], { mustNotPersist: true })

    await flushPersistedQueryCache(client)
    const persisted = await get('mobius-query-cache')
    assert.equal(typeof persisted, 'object',
      'IndexedDB should receive a structured value, not a main-thread JSON string')
    const keys = persisted.clientState.queries.map(q => q.queryKey)
    assert.deepEqual(keys, [['chat-messages', 'chat-1']])
    const data = persisted.clientState.queries[0].state.data
    assert.equal(data.offset, 4)
    assert.equal(data.messages.length, 30)
    assert.equal(data.messages[0].content, 'line-0')
  } finally {
    globalThis.indexedDB = previousIndexedDb
  }
})

test('persisted cache restore accepts both structured and legacy JSON values', () => {
  const persisted = {
    buster: 'v1',
    timestamp: 123,
    clientState: { queries: [] },
  }

  assert.equal(restorePersistedClient(persisted), persisted)
  assert.deepEqual(restorePersistedClient(JSON.stringify(persisted)), persisted)
})

test('reload handoff cannot be stranded by a blocked cache write', async () => {
  let fireDeadline = null
  let clearedTimer = null
  let completed = false
  const blockedWrite = new Promise(() => {})
  const handoff = awaitCacheFlushBeforeReload(blockedWrite, {
    timeoutMs: 25,
    setTimeoutFn: callback => {
      fireDeadline = callback
      return 17
    },
    clearTimeoutFn: timer => { clearedTimer = timer },
  }).then(() => { completed = true })

  await Promise.resolve()
  assert.equal(completed, false)
  assert.equal(typeof fireDeadline, 'function')
  fireDeadline()
  await handoff
  assert.equal(completed, true)
  assert.equal(clearedTimer, 17)
})
