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
import { QueryClient } from '@tanstack/react-query'
import { indexedDB } from 'fake-indexeddb'
import { get } from 'idb-keyval'
import {
  awaitCacheFlushBeforeReload,
  compactPersistedChatDetails,
  flushPersistedQueryCache,
  retainChatDetailQuery,
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

test('persistence projects every chat to one activation page without an entry ceiling', () => {
  const queries = ['a', 'b'].map(id => ({
    queryKey: ['chat-messages', id],
    state: {
      data: {
        messages: Array.from({ length: 28 }, (_, i) => ({ content: `${id}-${i}` })),
        offset: 4,
      },
    },
  }))
  const compacted = compactPersistedChatDetails({ clientState: { queries } })
  assert.equal(compacted.clientState.queries.length, 2)
  for (const query of compacted.clientState.queries) {
    assert.equal(query.state.data.messages.length, 20)
    assert.equal(query.state.data.offset, 12)
  }
  assert.equal(queries[0].state.data.messages.length, 28,
    'persistence projection never mutates the live cache')
})

test('the last mounted reader release compacts only that inactive working set', async () => {
  const client = new QueryClient()
  const messages = Array.from({ length: 30 }, (_, i) => ({ content: String(i) }))
  client.setQueryData(['chat-messages', 'chat-1'], { messages, offset: 3 })
  const releaseOne = retainChatDetailQuery(client, 'chat-1')
  const releaseTwo = retainChatDetailQuery(client, 'chat-1')
  releaseOne()
  assert.equal(client.getQueryData(['chat-messages', 'chat-1']).messages.length, 30)
  releaseTwo()
  await Promise.resolve()
  const compacted = client.getQueryData(['chat-messages', 'chat-1'])
  assert.equal(compacted.messages.length, 20)
  assert.equal(compacted.offset, 13)
})

test('a same-tick owner handoff cancels inactive compaction', async () => {
  const client = new QueryClient()
  const messages = Array.from({ length: 30 }, (_, i) => ({ content: String(i) }))
  client.setQueryData(['chat-messages', 'chat-1'], { messages, offset: 0 })
  const releaseFirstMount = retainChatDetailQuery(client, 'chat-1')
  releaseFirstMount()
  const releaseSecondMount = retainChatDetailQuery(client, 'chat-1')
  await Promise.resolve()
  assert.equal(client.getQueryData(['chat-messages', 'chat-1']).messages.length, 30,
    'StrictMode cleanup/setup and owner handoffs keep the live working set')
  releaseSecondMount()
  await Promise.resolve()
  assert.equal(client.getQueryData(['chat-messages', 'chat-1']).messages.length, 20)
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
