/* Clearing notifications refreshes history without blanking already loaded pages. */
import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { InfiniteQueryObserver, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { notificationQueries } from '../queries.js'
import useNotificationCenter from '../../components/NotificationBell/useNotificationCenter.js'

function actionsFor(client) {
  let actions
  function Center() {
    actions = useNotificationCenter(client).actions
    return null
  }
  renderToStaticMarkup(React.createElement(
    QueryClientProvider, { client }, React.createElement(Center),
  ))
  return actions
}

function fixture() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  const history = {
    pages: [Array.from({ length: 8 }, (_, id) => ({ id: String(id), sent_at: '2026-10-12T12:00:00Z' })), [{ id: 'older' }]],
    pageParams: [null, { id: '7', sentAt: '2026-10-12T12:00:00Z' }],
  }
  client.setQueryData(notificationQueries.list.key, history)
  client.setQueryData(notificationQueries.unreadCount.key, 0)
  client.setQueryData(notificationQueries.newCount.key, 0)
  let finishFetch
  let startedFetch
  const fetching = new Promise(resolve => { startedFetch = resolve })
  const response = new Promise(resolve => { finishFetch = resolve })
  const observer = new InfiniteQueryObserver(client, {
    ...notificationQueries.list.options,
    staleTime: Infinity,
    queryFn: () => { startedFetch(); return response },
  })
  const updates = []
  const unsubscribe = observer.subscribe(result => updates.push(result))
  return { client, history, observer, updates, fetching, finishFetch, cleanup() { unsubscribe(); client.clear() } }
}

for (const action of ['dismiss', 'clearAll']) {
  test(`${action} keeps paginated history visible until refreshed results arrive`, async () => {
    const f = fixture()
    const original = api.notifications[action]
    api.notifications[action] = async () => ({ ok: true })
    try {
      const work = actionsFor(f.client)[action]('0')
      await f.fetching
      assert.deepEqual(f.observer.getCurrentResult().data, f.history)
      assert.equal(f.observer.getCurrentResult().isLoading, false)
      // The server remains authoritative, including protected Undo rows.
      const remaining = action === 'dismiss' ? [{ id: 'older' }] : [{ id: 'active-undo' }]
      f.finishFetch(remaining)
      await work
      assert.deepEqual(f.client.getQueryData(notificationQueries.list.key).pages, [remaining])
      assert.ok(f.updates.length > 0)
      assert.ok(f.updates.every(result => result.data !== undefined && !result.isLoading))
    } finally {
      api.notifications[action] = original
      f.cleanup()
    }
  })

  test(`${action} failure preserves history and does not start a refresh`, async () => {
    const f = fixture()
    const original = api.notifications[action]
    api.notifications[action] = async () => { throw new Error('offline') }
    try {
      await assert.rejects(actionsFor(f.client)[action]('0'), /offline/)
      assert.deepEqual(f.client.getQueryData(notificationQueries.list.key), f.history)
      assert.equal(f.observer.getCurrentResult().isFetching, false)
    } finally {
      api.notifications[action] = original
      f.cleanup()
    }
  })
}
