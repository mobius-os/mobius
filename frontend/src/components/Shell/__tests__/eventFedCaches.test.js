import test from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, QueryObserver } from '@tanstack/query-core'

import { invalidateEventFedCachesAfterReconnect } from '../eventFedCaches.js'

const KEY = ['auth', 'providers', 'status']

for (const queryKey of [
  ['theme'], ['models', 'registry'], KEY, ['app-source', '1'],
  ['chat-app-artifacts', 'chat'], ['chat-activity', 'chat'],
  ['projects', 'files', 'project'], ['projects', 'git', 'project'],
]) {
  test(`reconnect refetches missed updates for ${queryKey.join('/')}`, async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
    let serverValue = 'before disconnect'
    let reads = 0
    const observer = new QueryObserver(client, {
      queryKey,
      queryFn: async () => { reads += 1; return serverValue },
      staleTime: Infinity,
    })
    const unsubscribe = observer.subscribe(() => {})
    try {
      await observer.refetch()
      assert.equal(client.getQueryData(queryKey), serverValue)
      serverValue = 'changed while disconnected'
      await Promise.all(invalidateEventFedCachesAfterReconnect(client))
      assert.equal(client.getQueryData(queryKey), serverValue)
      assert.equal(reads, 2)
    } finally {
      unsubscribe()
      client.clear()
    }
  })
}
