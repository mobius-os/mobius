import assert from 'node:assert/strict'
import test from 'node:test'

import { chatQueries } from '../../../hooks/queries.js'
import { prefetchChatMessages } from '../../Drawer/prefetchChatMessages.js'

test('drawer chat intent warms the exact detail query ChatView consumes', async () => {
  let options
  const queryClient = {
    prefetchQuery(value) {
      options = value
      return Promise.resolve()
    },
  }

  await prefetchChatMessages(queryClient, 'chat-123')

  assert.deepEqual(options.queryKey, chatQueries.messages.key('chat-123'))
  assert.equal(options.staleTime, 30_000)
  assert.equal(typeof options.queryFn, 'function')
})
