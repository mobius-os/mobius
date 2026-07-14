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

const QUERY_CACHE_KEY = 'mobius-query-cache'
const QUERY_CACHE_BUSTER = 'v1'

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

export const queryPersister = createAsyncStoragePersister({
  storage: idbStorage,
  key: QUERY_CACHE_KEY,
  throttleTime: 1000,
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
//   - ['auth','provider','claude-status']   → Claude connected state
//   - ['auth','providers','status']         → all-providers connected state
// Matched by full key, not by ['auth'] alone, so the short-lived
// setup-status query (['auth','setup','status']) is NOT persisted.
const PERSISTED_FULL_KEYS = new Set([
  JSON.stringify(['settings']),
  JSON.stringify(['auth', 'provider', 'claude-status']),
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
  await idbStorage.setItem(QUERY_CACHE_KEY, JSON.stringify(persistedClient))
}
