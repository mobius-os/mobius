import assert from 'node:assert/strict'
import test from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { InfiniteQueryObserver, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { notificationQueries } from '../../hooks/queries.js'
import NotificationsView from '../../components/NotificationsView/NotificationsView.jsx'
import {
  completeNotificationRecovery,
  notificationRecoveryAction,
  parseNotificationRecoveryAction,
  recoveryFailure,
  recoveryUnavailableLabel,
} from '../notificationRecovery.js'

const deletedAt = '2026-09-19T20:00:00Z'
const expiresAt = '2026-09-26T20:00:00Z'
const receipt = (overrides = {}) => ({
  action: 'recover_chat', title: 'Undo', resource_type: 'chat', resource_id: 'chat-123',
  deleted_at: deletedAt, expires_at: expiresAt, ...overrides,
})

test('recovery notifications require a matching tombstone-bound resource action', () => {
  assert.deepEqual(parseNotificationRecoveryAction(receipt()), {
    action: 'recover_chat', title: 'Undo', resourceType: 'chat', resourceId: 'chat-123',
    completedAt: null, expiresAt,
  })
  for (const fields of [
    { resource_type: 'app' }, { resource_id: '../7' }, { target: '/shell/?chat=x' },
    { completed_at: 'not-a-date' }, { deleted_at: null }, { expires_at: null },
    { expires_at: deletedAt },
  ]) assert.equal(parseNotificationRecoveryAction(receipt(fields)), null)
})

test('completed and expired receipts remain inspectable but are not actionable', () => {
  const pending = notificationRecoveryAction({ actions: [receipt()] })
  assert.equal(recoveryUnavailableLabel(pending, Date.parse(expiresAt) - 1), null)
  assert.equal(recoveryUnavailableLabel(pending, Date.parse(expiresAt)), 'Recovery window expired')
  const completed = notificationRecoveryAction({ actions: [receipt({ completed_at: deletedAt })] })
  assert.equal(recoveryUnavailableLabel(completed, Date.parse(expiresAt)), 'Restored')
})

test('recovery labels distinguish terminal expiry and superseded receipts from retryable failures', () => {
  assert.deepEqual(recoveryFailure({ status: 410 }), { terminal: true, message: 'Recovery window expired' })
  assert.equal(recoveryFailure({ code: 'recovery_superseded' }).message, 'Earlier deletion — use the latest Undo')
  assert.equal(recoveryFailure({ code: 'recovery_already_restored' }).message, 'Already restored')
  assert.equal(recoveryFailure({ status: 404 }).message, 'Recovery no longer available')
  assert.equal(recoveryFailure(new Error('Offline')).terminal, false)
})

test('receipt completion updates an older history page without resurrecting cleared history', () => {
  const newer = { id: 'new', actions: [] }
  const history = { pages: [[newer], [{ id: 'old', actions: [receipt()] }]], pageParams: [null, 'new'] }
  const completed = completeNotificationRecovery(history, 'old', deletedAt)
  assert.equal(completed.pages[0][0], newer)
  assert.equal(completed.pages[1][0].actions[0].completed_at, deletedAt)
  assert.equal(history.pages[1][0].actions[0].completed_at, undefined)
  const cleared = { pages: [[]], pageParams: [null] }
  assert.deepEqual(completeNotificationRecovery(cleared, 'old', deletedAt), cleared)
})

test('notification history follows server cursors beyond the newest eight and retains pages on refresh', async (t) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const original = api.notifications.list
  const notifications = Array.from({ length: 20 }, (_, i) => ({ id: `n-${i}`, actions: [] }))
  notifications.at(-1).actions = [receipt()]
  const requests = []
  api.notifications.list = async ({ before, limit }) => {
    requests.push(before)
    const start = before ? notifications.findIndex(n => n.id === before) + 1 : 0
    return { ok: true, json: async () => notifications.slice(start, start + limit) }
  }
  t.after(() => { api.notifications.list = original; queryClient.clear() })
  const observer = new InfiniteQueryObserver(queryClient, notificationQueries.list.options)
  await observer.refetch()
  await observer.fetchNextPage()
  const result = await observer.fetchNextPage()
  assert.deepEqual(requests, [null, 'n-7', 'n-15'])
  assert.equal(result.hasNextPage, false)
  assert.equal(result.data.pages.flat().length, 20)
  assert.equal(notificationRecoveryAction(result.data.pages.flat().at(-1)).resourceId, 'chat-123')
  const refreshed = await observer.refetch()
  assert.equal(refreshed.data.pages.flat().length, 20)
})

test('rendered history disables expired Undo and offers older pages', () => {
  const queryClient = new QueryClient()
  const expired = receipt({ deleted_at: '2020-01-01T00:00:00Z', expires_at: '2020-01-08T00:00:00Z' })
  const rows = Array.from({ length: 8 }, (_, i) => ({
    id: `n-${i}`, title: 'Chat deleted', source_type: 'shell', sent_at: deletedAt,
    actions: i === 0 ? [expired] : [],
  }))
  queryClient.setQueryData(notificationQueries.list.key, { pages: [rows], pageParams: [null] })
  const html = renderToStaticMarkup(React.createElement(QueryClientProvider, { client: queryClient },
    React.createElement(NotificationsView, { onRecoveryAction() {} }),
  ))
  assert.match(html, /Recovery window expired/)
  assert.doesNotMatch(html, />Undo</)
  assert.match(html, /Load older notifications/)
  queryClient.clear()
})
