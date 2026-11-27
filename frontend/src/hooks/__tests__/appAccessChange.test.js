/* Accepting or revoking an app's access changes which project types New project offers, so the shell reads them again. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { QueryClient } from '@tanstack/react-query'
import { appQueries, projectQueries } from '../queries.js'

const stale = (queryClient, key) => queryClient.getQueryState(key).isInvalidated

test('an access change marks the app’s consent and the project types for a fresh read', async () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  queryClient.setQueryData(appQueries.consent.key('7'), { status: 'required' })
  queryClient.setQueryData(appQueries.consent.key('8'), { status: 'accepted' })
  queryClient.setQueryData(projectQueries.templates.key, [{ id: 'notes' }])
  queryClient.setQueryData(projectQueries.keys.all, [{ id: 'p1' }])

  await appQueries.consent.changed(queryClient, '7')

  assert.equal(stale(queryClient, appQueries.consent.key('7')), true)
  assert.equal(stale(queryClient, projectQueries.templates.key), true)
  assert.equal(stale(queryClient, appQueries.consent.key('8')), false)
  assert.equal(stale(queryClient, projectQueries.keys.all), false)
})
