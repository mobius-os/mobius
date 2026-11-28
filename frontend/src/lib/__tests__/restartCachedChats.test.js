/**
 * While the server restarts, chats the shell already cached stay readable:
 * the drawer list and a chat's transcript survive a reload from IndexedDB and
 * a failing server, read-only, instead of turning into an error or a blank.
 *
 *   cd frontend && npm run test:lib
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, dehydrate } from '@tanstack/react-query'
import { persistQueryClientRestore } from '@tanstack/react-query-persist-client'
import { chatMessagesQueryKey, chatQueries } from '../../hooks/queries.js'
import { chatCacheEntryState, chatDetailCacheValue } from '../chatDetailCache.js'
import { compactPersistedChatDetails, persistOptions } from '../../queryClient.js'
import { activationServerUnreachable } from '../../components/ChatView/chatRuntimeState.js'

const transcript = Array.from({ length: 40 }, (_, index) => ({
  id: `m-${index}`, role: index % 2 ? 'assistant' : 'user', content: `line ${index}`,
}))

/** The client a reopened PWA restores from the persisted snapshot. */
async function reopenedFromDisk(source) {
  const persisted = compactPersistedChatDetails({
    buster: 'v1',
    timestamp: Date.now(),
    clientState: dehydrate(source, persistOptions.dehydrateOptions),
  })
  const restored = new QueryClient()
  await persistQueryClientRestore({
    ...persistOptions,
    queryClient: restored,
    persister: { restoreClient: async () => persisted, removeClient: async () => {} },
  })
  return restored
}

function restartingServer(t) {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => Response.json({ detail: 'restarting' }, { status: 502 })
  t.after(() => { globalThis.fetch = originalFetch })
}

test('a reopened shell keeps the cached chat list while the server restarts', async t => {
  const source = new QueryClient()
  source.setQueryData(chatQueries.list.key, [{ id: 'chat-1', title: 'Kept during restart' }])
  const client = await reopenedFromDisk(source)
  restartingServer(t)

  await assert.rejects(client.fetchQuery({
    queryKey: chatQueries.list.key,
    queryFn: context => chatQueries.list.fetch(context),
    retry: false,
    staleTime: 0,
  }))
  const state = client.getQueryState(chatQueries.list.key)
  assert.equal(state.status, 'error', 'the failed read is reported, not hidden')
  assert.deepEqual(state.data, [{ id: 'chat-1', title: 'Kept during restart' }],
    'the failed read never replaces the last-known rows')
})

test('a reopened shell can still paint a cached transcript from its persisted tail', async () => {
  const source = new QueryClient()
  source.setQueryData(chatMessagesQueryKey('chat-1'), chatDetailCacheValue({
    offset: 0, total: transcript.length, messages: transcript,
  }))
  const client = await reopenedFromDisk(source)
  const cached = client.getQueryData(chatMessagesQueryKey('chat-1'))

  assert.equal(cached.messages.at(-1).content, 'line 39')
  assert.equal(chatCacheEntryState(cached), 'paintable',
    'with no server, the persisted tail is a complete window ChatView may show')
  assert.equal(chatCacheEntryState(cached, 'm-35'), 'paintable',
    'a reading position inside the persisted tail still paints')
  // A reading position older than the persisted tail cannot be honoured from
  // disk. While the server could answer, that chat still waits for it rather
  // than opening at the wrong place; during a restart ChatView opens these
  // latest messages instead and restores the saved position once it answers.
  assert.equal(chatCacheEntryState(cached, 'm-3'), 'missing')
  assert.equal(chatCacheEntryState(cached), 'paintable',
    'the latest cached window is what a restart opens for that chat')
})

test('only an unreachable server opens the latest cached messages in place of a saved position', () => {
  const unreachable = [
    new TypeError('Failed to fetch'),
    Object.assign(new Error('timed out'), { name: 'TimeoutError' }),
    new Error('CHAT_LOAD_FAILED_502'),
    new Error('CHAT_RUNTIME_FAILED_503'),
    new Error('CHAT_LOAD_FAILED_408'),
  ]
  for (const error of unreachable) assert.equal(activationServerUnreachable(error), true, error.message)
  const answered = [
    new Error('CHAT_NOT_FOUND'),
    new Error('CHAT_READING_ANCHOR_NOT_FOUND'),
    new Error('CHAT_LOAD_FAILED_403'),
    new Error('CHAT_LOAD_FAILED_429'),
    new Error('CHAT_RUNTIME_OUT_OF_ORDER'),
  ]
  for (const error of answered) assert.equal(activationServerUnreachable(error), false, error.message)
})
