import test from 'node:test'
import assert from 'node:assert/strict'
import { authQueries } from '../../hooks/queries.js'

test('markConnected publishes durable auth immediately and revalidates off-path', () => {
  let data = {
    codex: { configured: false, authenticated: false, detail: 'stale' },
    claude: { configured: true, authenticated: true },
  }
  let invalidatedKey = null
  const queryClient = {
    setQueryData(key, update) {
      assert.deepEqual(key, authQueries.provider.statuses.key)
      data = update(data)
    },
    invalidateQueries({ queryKey }) {
      invalidatedKey = queryKey
      return Promise.resolve()
    },
  }

  authQueries.provider.statuses.markConnected(queryClient, 'codex')

  assert.deepEqual(data, {
    codex: {
      configured: true,
      authenticated: true,
      detail: 'stale',
    },
    claude: { configured: true, authenticated: true },
  })
  assert.deepEqual(invalidatedKey, authQueries.provider.statuses.key)
})

test('markConnected supports future provider ids without a registry edit', () => {
  let data
  const queryClient = {
    setQueryData(_key, update) { data = update(undefined) },
    invalidateQueries() { return Promise.resolve() },
  }

  authQueries.provider.statuses.markConnected(queryClient, 'future-provider')

  assert.deepEqual(data, {
    'future-provider': { configured: true, authenticated: true },
  })
})
