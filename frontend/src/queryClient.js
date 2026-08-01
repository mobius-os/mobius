/**
 * TanStack Query setup with IndexedDB persistence.
 *
 * The QueryClient is the canonical client cache for everything fetched
 * from the server (theme, chats, messages, apps, owner). Components
 * subscribe via useQuery hooks and re-render automatically when the
 * cache for their query key changes — no manual fetch+useState in
 * consumers, no per-component loading flash on mount.
 *
 * Persistence (`createAsyncStoragePersister` + `idb-keyval`) mirrors
 * the in-memory cache to IndexedDB. After a reload, queries hydrate
 * from disk before any network round-trip — chats and messages
 * appear instantly, then revalidate in background.
 *
 * `defaultOptions` are tuned to "cached but not stale": staleTime 30s
 * means data is considered fresh for 30s (no refetch on remount in
 * that window), gcTime 24h means it's kept on disk for a day after
 * last use. Tweak per-query via the queryKey/queryFn config.
 */
import { QueryClient, dehydrate } from '@tanstack/react-query'
import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import { get, set, del } from 'idb-keyval'
import { compactChatDetailCacheValue } from './lib/chatDetailCache.js'

const QUERY_CACHE_KEY = 'mobius-query-cache'
const QUERY_CACHE_BUSTER = 'v1'
export const QUERY_CACHE_RELOAD_FLUSH_TIMEOUT_MS = 750
const mountedChatDetails = new WeakMap()

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      gcTime: 24 * 60 * 60 * 1000,
      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
      retry: 1,
    },
  },
})

function mountedChatDetailCount(client, chatId) {
  return mountedChatDetails.get(client)?.get(String(chatId)) || 0
}

/**
 * Retain the complete loaded window only while a ChatView owns it.
 *
 * This is working-set ownership rather than an arbitrary cache-size policy:
 * every mounted/hidden pane keeps its exact pagination and scroll state; the
 * moment the final owner releases, that one inactive entry returns to the
 * server's ordinary 20-message activation page.
 */
export function retainChatDetailQuery(client, chatId) {
  const id = String(chatId || '')
  if (!client || !id) return () => {}
  let counts = mountedChatDetails.get(client)
  if (!counts) {
    counts = new Map()
    mountedChatDetails.set(client, counts)
  }
  counts.set(id, (counts.get(id) || 0) + 1)
  let released = false
  return () => {
    if (released) return
    released = true
    const current = mountedChatDetailCount(client, id)
    if (current <= 1) counts.delete(id)
    else counts.set(id, current - 1)
    if (counts.size === 0) mountedChatDetails.delete(client)
    if (current > 1) return

    // React StrictMode deliberately runs mount effects through a synthetic
    // setup -> cleanup -> setup cycle. Let that same-tick handoff (and a real
    // cross-world owner handoff) re-retain the chat before deciding it is
    // inactive; otherwise the optimization would compact a view that never
    // actually left the screen.
    const compactIfStillUnowned = () => {
      if (mountedChatDetailCount(client, id) > 0) return
      const query = client.getQueryCache().find({
        queryKey: ['chat-messages', id],
        exact: true,
      })
      if (!query || query.state.fetchStatus !== 'idle') return
      const compacted = compactChatDetailCacheValue(query.state.data)
      if (compacted === query.state.data) return
      query.setData(compacted, {
        manual: true,
        updatedAt: query.state.dataUpdatedAt,
      })
    }
    if (typeof queueMicrotask === 'function') queueMicrotask(compactIfStillUnowned)
    else Promise.resolve().then(compactIfStillUnowned)
  }
}

/** Persistence-only projection: all visited chats may remain warm, but each
 * snapshot stores only the same recent page a cold activation asks the server
 * for. The live QueryClient is never changed here. */
export function compactPersistedChatDetails(persistedClient) {
  const queries = persistedClient?.clientState?.queries
  if (!Array.isArray(queries)) return persistedClient
  return {
    ...persistedClient,
    clientState: {
      ...persistedClient.clientState,
      queries: queries.map(query => (
        query?.queryKey?.[0] !== 'chat-messages'
          ? query
          : {
              ...query,
              state: {
                ...query.state,
                data: compactChatDetailCacheValue(query.state?.data),
              },
            }
      )),
    },
  }
}

const idbStorage = {
  getItem: (key) => get(key),
  setItem: (key, value) => set(key, value),
  removeItem: (key) => del(key),
}

export const queryPersister = createAsyncStoragePersister({
  storage: idbStorage,
  key: QUERY_CACHE_KEY,
  throttleTime: 1000,
  serialize: persistedClient => JSON.stringify(
    compactPersistedChatDetails(persistedClient),
  ),
  // Repair any unbounded snapshot written by an older shell before it reaches
  // React. This makes the correction take effect on the first reload instead
  // of waiting for every historical chat to be revisited.
  deserialize: value => compactPersistedChatDetails(JSON.parse(value)),
})

export const persistOptions = {
  persister: queryPersister,
  maxAge: 24 * 60 * 60 * 1000,
  buster: QUERY_CACHE_BUSTER,
  dehydrateOptions: {
    shouldDehydrateQuery: (query) => shouldPersistQueryKey(query.queryKey),
  },
}

// Decide whether a query's cache entry is mirrored to IndexedDB.
//
// Top-level domains (chats, messages, theme, apps) match on the first
// key segment. The Settings view's provider/CLI-version/status queries
// are persisted too so the panel paints from disk on open instead of
// flashing an empty providers list while the live probe revalidates:
//   - ['settings']                          → provider config + CLI versions
//   - ['auth','providers','status']         → canonical provider state
// Matched by full key, not by ['auth'] alone, so the short-lived
// setup-status query (['auth','setup','status']) is NOT persisted.
const PERSISTED_FULL_KEYS = new Set([
  JSON.stringify(['settings']),
  JSON.stringify(['auth', 'providers', 'status']),
])

export function shouldPersistQueryKey(queryKey) {
  const head = queryKey[0]
  if (head === 'chats' || head === 'chat-messages' || head === 'theme' || head === 'apps') {
    return true
  }
  return PERSISTED_FULL_KEYS.has(JSON.stringify(queryKey))
}

/** Force the current in-memory cache to IndexedDB before an intentional shell
 * reload. Normal persistence is deliberately throttled because live streams
 * update the chat cache frequently. A deferred shell rebuild can become idle
 * in the sub-second interval after terminal promotion, however; reloading in
 * that window used to hydrate the previous partial and make the final response
 * disappear until the chat remounted/refetched. This explicit terminal
 * handoff snapshots the same allowlisted cache without changing steady-state
 * throttling. */
export async function flushPersistedQueryCache(client = queryClient) {
  const persistedClient = compactPersistedChatDetails({
    buster: QUERY_CACHE_BUSTER,
    timestamp: Date.now(),
    clientState: dehydrate(client, {
      shouldDehydrateQuery: (query) => shouldPersistQueryKey(query.queryKey),
    }),
  })
  await idbStorage.setItem(QUERY_CACHE_KEY, JSON.stringify(persistedClient))
}

/** Wait briefly for the reload handoff, but never let a blocked IndexedDB
 * transaction strand a waiting service-worker generation. The write itself is
 * still allowed to finish; this only bounds how long shell activation waits. */
export function awaitCacheFlushBeforeReload(
  flushPromise,
  {
    timeoutMs = QUERY_CACHE_RELOAD_FLUSH_TIMEOUT_MS,
    setTimeoutFn = (typeof setTimeout !== 'undefined' ? setTimeout : null),
    clearTimeoutFn = (typeof clearTimeout !== 'undefined' ? clearTimeout : null),
  } = {},
) {
  if (!setTimeoutFn || !Number.isFinite(timeoutMs) || timeoutMs < 0) {
    return Promise.resolve(flushPromise).catch(() => {})
  }
  return new Promise(resolve => {
    let settled = false
    let timer = null
    const finish = () => {
      if (settled) return
      settled = true
      if (timer != null && clearTimeoutFn) clearTimeoutFn(timer)
      resolve()
    }
    timer = setTimeoutFn(finish, timeoutMs)
    Promise.resolve(flushPromise).then(finish, finish)
  })
}
