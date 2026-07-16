import assert from 'node:assert/strict'
import test from 'node:test'

import {
  drainCreatedChats,
  registerCreatedChats,
} from './_chatFixtureRegistry.mjs'

test('registry drains only exact IDs registered by one worker', () => {
  registerCreatedChats(3, ['chat-a', { id: 'chat-b' }, 'chat-a', null])
  registerCreatedChats(4, 'other-worker-chat')

  assert.deepEqual(drainCreatedChats(3), ['chat-a', 'chat-b'])
  assert.deepEqual(drainCreatedChats(3), [])
  assert.deepEqual(drainCreatedChats(4), ['other-worker-chat'])
})
