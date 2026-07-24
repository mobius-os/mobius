import { test } from 'node:test'
import assert from 'node:assert/strict'

import { invalidateShellListCache } from '../../api/client.js'
import { SHELL_DATA_CACHE } from '../../sw-cache-policy.js'

test('list invalidation evicts the exact shared offline projection', async () => {
  const calls = []
  const cacheStorage = {
    async open(name) {
      calls.push(['open', name])
      return {
        async delete(url) {
          calls.push(['delete', url])
          return true
        },
      }
    },
  }

  assert.equal(await invalidateShellListCache('chats', {
    cacheStorage,
    origin: 'https://mobius.test',
  }), true)
  assert.equal(await invalidateShellListCache('apps', {
    cacheStorage,
    origin: 'https://mobius.test',
  }), true)
  assert.deepEqual(calls, [
    ['open', SHELL_DATA_CACHE],
    ['delete', 'https://mobius.test/api/chats'],
    ['open', SHELL_DATA_CACHE],
    ['delete', 'https://mobius.test/api/apps/'],
  ])
})

test('list invalidation is best-effort and rejects unknown projections', async () => {
  const broken = {
    async open() { throw new Error('quota/storage unavailable') },
  }
  assert.equal(await invalidateShellListCache('chats', {
    cacheStorage: broken,
    origin: 'https://mobius.test',
  }), false)
  assert.equal(await invalidateShellListCache('other', {
    cacheStorage: broken,
    origin: 'https://mobius.test',
  }), false)
})
