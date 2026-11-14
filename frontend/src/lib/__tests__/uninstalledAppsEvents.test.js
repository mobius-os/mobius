import assert from 'node:assert/strict'
import test from 'node:test'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { appQueries } from '../../hooks/queries.js'
import { refreshUninstalledForSystemEvent } from '../../components/Drawer/uninstalledAppsEvents.js'

// An observer stands in for the open Apps screen: its list is refetched exactly when the query is invalidated.
async function withOpenList(run) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  let fetches = 0
  const observer = new QueryObserver(client, {
    queryKey: appQueries.uninstalled.key,
    queryFn: async () => { fetches += 1; return [] },
    staleTime: Infinity,
  })
  const unsubscribe = observer.subscribe(() => {})
  try {
    await new Promise(resolve => setTimeout(resolve, 0))
    assert.equal(fetches, 1, 'the open list loaded once')
    await run(client, () => fetches)
  } finally {
    unsubscribe()
    client.clear()
  }
}

for (const type of ['app_deleted', 'app_recovered']) {
  test(`an ${type} event refreshes the list while the Apps screen is open`, async () => {
    await withOpenList(async (client, fetches) => {
      await refreshUninstalledForSystemEvent(client, { type, appId: '7' })
      assert.equal(fetches(), 2)
    })
  })
}

test('other events leave the list alone', async () => {
  await withOpenList(async (client, fetches) => {
    for (const event of [{ type: 'app_updated', appId: '7' }, { type: 'chat_renamed' }, null, undefined]) {
      await refreshUninstalledForSystemEvent(client, event)
    }
    assert.equal(fetches(), 1)
  })
})
