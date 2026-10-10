import test from 'node:test'
import assert from 'node:assert/strict'
import {
  chatEditDiffsQueryKey,
  invalidateChatChangesQueries,
} from '../chatChangesQueries.js'

test('chat completion refreshes only that chat’s recorded edits', async () => {
  const calls = []
  const queryClient = {
    invalidateQueries: async options => { calls.push(options) },
  }

  await invalidateChatChangesQueries(queryClient, 'chat-a')

  assert.deepEqual(calls, [{
    queryKey: chatEditDiffsQueryKey('chat-a'),
    exact: true,
  }])
})
