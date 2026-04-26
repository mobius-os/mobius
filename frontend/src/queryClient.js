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
import { QueryClient } from '@tanstack/react-query'
import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import { get, set, del } from 'idb-keyval'

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
  key: 'mobius-query-cache',
  throttleTime: 1000,
})

export const persistOptions = {
  persister: queryPersister,
  maxAge: 24 * 60 * 60 * 1000,
  buster: 'v1',
  dehydrateOptions: {
    shouldDehydrateQuery: (query) => {
      const k = query.queryKey[0]
      return k === 'chats' || k === 'chat-messages' || k === 'theme' || k === 'apps'
    },
  },
}
