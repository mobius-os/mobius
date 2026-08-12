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
 * the in-memory cache to IndexedDB using IndexedDB's native structured clone.
 * After a reload, queries hydrate from disk before any network round-trip —
 * chats and messages appear instantly, then revalidate in background.
 *
 * `defaultOptions` are tuned to "cached but not stale": staleTime 30s
 * means data is considered fresh for 30s (no refetch on remount in
 * that window), while gcTime 24h bounds inactive queries during a live
 * session. The last persisted snapshot itself does not expire by age: it is
 * the shell's durable offline fallback and is explicitly erased on logout.
 * Tweak per-query via the queryKey/queryFn config.
 */
import { QueryClient, dehydrate } from '@tanstack/react-query'
import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import { get, set, del } from 'idb-keyval'
import { compactPersistedChatDetailCacheValue } from './lib/chatDetailCache.js'

const QUERY_CACHE_KEY = 'mobius-query-cache'
const QUERY_CACHE_BUSTER = 'v1'
export const QUERY_CACHE_RELOAD_FLUSH_TIMEOUT_MS = 750

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

const idbStorage = {
  getItem: (key) => get(key),
  setItem: (key, value) => set(key, value),
  removeItem: (key) => del(key),
}

/** Bound steady-state IndexedDB growth without changing the live query cache.
 * An explicit shell reload writes the full current window separately below. */
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
                data: compactPersistedChatDetailCacheValue(query.state?.data),
              },
            }
      )),
    },
  }
}

// Earlier releases stored this value as one large JSON string. Keep restore
// compatible with that on-disk shape while writing the structured value
// directly from now on. Avoiding JSON.stringify on the main thread matters on
// large chat histories: the throttled save can otherwise land between touch
// start and the browser's first native scroll frame.
export function restorePersistedClient(storedClient) {
  return typeof storedClient === 'string'
    ? JSON.parse(storedClient)
    : storedClient
}

export const queryPersister = createAsyncStoragePersister({
  storage: idbStorage,
  key: QUERY_CACHE_KEY,
  throttleTime: 1000,
  serialize: compactPersistedChatDetails,
  deserialize: restorePersistedClient,
})

export const persistOptions = {
  persister: queryPersister,
  // Last-known owner state is more useful than a blank shell after a long
  // offline stretch. Every restored query revalidates normally when the server
  // returns, and logout explicitly deletes this owner-scoped database.
  maxAge: Infinity,
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
  const persistedClient = {
    buster: QUERY_CACHE_BUSTER,
    timestamp: Date.now(),
    clientState: dehydrate(client, {
      shouldDehydrateQuery: (query) => shouldPersistQueryKey(query.queryKey),
    }),
  }
  await idbStorage.setItem(QUERY_CACHE_KEY, persistedClient)
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
