/**
 * While the server restarts, what the shell already cached stays usable: the
 * drawer list, a chat's transcript and the owner's model choices survive a
 * reload from IndexedDB and a failing server instead of turning into an error
 * or a blank. These tests cover the cache, its persistence and the pure
 * activation decisions ChatView applies (chatRuntimeState.js); the source
 * contracts that ChatView applies them live in Shell/__tests__/chatHandoff.
 *
 *   cd frontend && npm run test:lib
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, dehydrate } from '@tanstack/react-query'
import { persistQueryClientRestore } from '@tanstack/react-query-persist-client'
import { chatMessagesQueryKey, chatQueries, modelQueries } from '../../hooks/queries.js'
import { chatCacheEntryState, chatDetailCacheValue } from '../chatDetailCache.js'
import { compactPersistedChatDetails, persistOptions } from '../../queryClient.js'
import {
  activationKeepsOfflineFrame,
  activationServerUnreachable,
  opensOfflineLatestFrame,
} from '../../components/ChatView/chatRuntimeState.js'

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

/** A persisted chat whose saved reading position is older than its tail. */
async function reopenedChatWithOldReadingPosition() {
  const source = new QueryClient()
  source.setQueryData(chatMessagesQueryKey('chat-1'), chatDetailCacheValue({
    offset: 0, total: transcript.length, messages: transcript,
  }))
  const cached = (await reopenedFromDisk(source)).getQueryData(chatMessagesQueryKey('chat-1'))
  return { cacheState: chatCacheEntryState(cached, 'm-3'), latestCacheState: chatCacheEntryState(cached) }
}

test('a restart keeps the offline chat frame through each activation retry', async () => {
  const states = await reopenedChatWithOldReadingPosition()
  const restarting = new Error('CHAT_LOAD_FAILED_502')

  // First activation: nothing is on screen yet, so the chat waits for the server.
  assert.equal(activationKeepsOfflineFrame({ offlineFrameShown: false, ...states }), false)
  assert.equal(opensOfflineLatestFrame({ error: restarting, searchActivation: false, ...states }), true,
    'the failed read opens the latest cached messages')

  // Each quiet retry keeps that frame on screen instead of hiding it again,
  // and a retry that fails the same way reopens the same frame.
  for (let retry = 0; retry < 3; retry += 1) {
    assert.equal(activationKeepsOfflineFrame({ offlineFrameShown: true, ...states }), true, `retry ${retry}`)
    assert.equal(opensOfflineLatestFrame({ error: restarting, searchActivation: false, ...states }), true)
  }
})

test('the offline chat frame is kept only for the unresolved saved position it replaced', async () => {
  const states = await reopenedChatWithOldReadingPosition()
  assert.equal(opensOfflineLatestFrame({
    error: new Error('CHAT_LOAD_FAILED_403'), searchActivation: false, ...states,
  }), false, 'a server that answered could have resolved the position')
  assert.equal(opensOfflineLatestFrame({
    error: new Error('CHAT_LOAD_FAILED_502'), searchActivation: true, ...states,
  }), false, 'a search or decision reveal waits for its own row')
  assert.equal(opensOfflineLatestFrame({
    error: new Error('CHAT_LOAD_FAILED_502'), searchActivation: false,
    cacheState: 'missing', latestCacheState: 'missing',
  }), false, 'no cached window, nothing to open')
  assert.equal(activationKeepsOfflineFrame({
    offlineFrameShown: true, cacheState: 'paintable', latestCacheState: 'paintable',
  }), false, 'a cache covering the saved position needs no exception')
  assert.equal(activationKeepsOfflineFrame({
    offlineFrameShown: true, cacheState: 'missing', latestCacheState: 'missing',
  }), false, 'a frame whose cache is gone is not kept')
})

function failingPrefsServer(t) {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => Response.json({ detail: 'restarting' }, { status: 503 })
  t.after(() => { globalThis.fetch = originalFetch })
}

const prefsState = client => {
  const state = client.getQueryState(modelQueries.prefs.key)
  return { data: state?.data, isError: state?.status === 'error' }
}

test('the owner\'s model choices from before a failed read survive a reload during a restart', async t => {
  const known = { hidden_ids: ['hidden-model'], recent_ids: ['recent-model'],
    selected_agent: { provider: 'claude', model: 'recent-model', effort: 'high' } }
  const source = new QueryClient()
  source.setQueryData(modelQueries.prefs.key, known)
  failingPrefsServer(t)
  await assert.rejects(source.fetchQuery(modelQueries.prefs.options()))
  assert.equal(source.getQueryState(modelQueries.prefs.key).status, 'error')

  const client = await reopenedFromDisk(source)
  assert.deepEqual(client.getQueryData(modelQueries.prefs.key), known,
    'the failed read is persisted with the last-known choices beside it')
  assert.deepEqual(modelQueries.prefs.view(prefsState(client)), { prefs: known, unavailable: true },
    'the reopened picker uses them, still marked unconfirmed')
})

test('model choices never read before a restart reopen as show-all, unconfirmed', async t => {
  const source = new QueryClient()
  failingPrefsServer(t)
  await assert.rejects(source.fetchQuery(modelQueries.prefs.options()))

  const client = await reopenedFromDisk(source)
  assert.deepEqual(modelQueries.prefs.view(prefsState(client)),
    { prefs: { hidden_ids: [], recent_ids: [] }, unavailable: true })
})
