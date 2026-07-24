import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import {
  forgetConfirmedDeletion,
  forgetConfirmedDeletionIfExists,
  rememberConfirmedDeletion,
  withoutConfirmedDeletions,
} from '../confirmedDeletion.js'

test('confirmed deletion dominates a later stale-present list', () => {
  const deleted = new Set()
  const original = [{ id: 'a' }, { id: 'b' }]

  rememberConfirmedDeletion(deleted, 'b')
  assert.deepEqual(
    withoutConfirmedDeletions(original, deleted),
    [{ id: 'a' }],
  )
  // Numeric and string ids share one identity across app/chat projections.
  assert.deepEqual(
    withoutConfirmedDeletions([{ id: 2 }, { id: 3 }], new Set(['2'])),
    [{ id: 3 }],
  )
})

test('only confirmed recovery clears the tombstone', () => {
  const deleted = new Set(['gone'])
  forgetConfirmedDeletion(deleted, 'gone')
  assert.deepEqual(
    withoutConfirmedDeletions([{ id: 'gone' }], deleted),
    [{ id: 'gone' }],
  )
})

test('a reusable app id clears only after direct live-resource evidence', async () => {
  const deleted = new Set(['42'])
  const verdicts = []

  assert.equal(await forgetConfirmedDeletionIfExists(
    deleted,
    42,
    async id => {
      verdicts.push(id)
      return 'unknown'
    },
  ), false)
  assert.deepEqual([...deleted], ['42'])

  assert.equal(await forgetConfirmedDeletionIfExists(
    deleted,
    '42',
    async id => {
      verdicts.push(id)
      return 'deleted'
    },
  ), false)
  assert.deepEqual([...deleted], ['42'])

  assert.equal(await forgetConfirmedDeletionIfExists(
    deleted,
    42,
    async id => {
      verdicts.push(id)
      return 'exists'
    },
  ), true)
  assert.deepEqual([...deleted], [])
  assert.deepEqual(verdicts, ['42', '42', '42'])
})

test('Shell reconciles both query completion and direct mutation paths', () => {
  const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
  const queries = readFileSync(
    new URL('../../../hooks/queries.js', import.meta.url),
    'utf8',
  )

  assert.match(shell, /appsQuery = appQueries\.list\.useQuery\(\{ reconcile: reconcileApps \}\)/)
  assert.match(shell, /mergeChatListWithCreatedGuards[\s\S]*deletedChatIdsRef\.current/)
  assert.match(shell, /confirmChatDeleted\(id\)[\s\S]*showToast\('Chat deleted'/)
  assert.match(shell, /confirmAppDeleted\(id\)[\s\S]*showToast\('App deleted'/)
  assert.match(shell, /app_updated[\s\S]*confirmAppIdentityIsLive\(ev\.appId\)/)
  assert.match(shell, /reconcileSystemStateOnOpen[\s\S]*reconcileDeletedAppIdentities\(\)/)
  assert.match(queries, /function useAppsQuery\(\{ reconcile \} = \{\}\)/)
})
