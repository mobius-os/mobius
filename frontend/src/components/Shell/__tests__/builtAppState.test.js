import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  builtAppForChat,
  withBuiltAppForChat,
  withoutBuiltAppForChat,
} from '../builtAppState.js'

test('built apps are scoped to the chat that produced them', () => {
  const state = withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Habits' })

  assert.deepEqual(builtAppForChat(state, 'chat-a'), { id: 7, name: 'Habits' })
  assert.equal(builtAppForChat(state, 'chat-b'), null)
})

test('clearing one chat does not clear another chat preview', () => {
  const state = withBuiltAppForChat(
    withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Habits' }),
    'chat-b',
    { id: 8, name: 'Notes' },
  )

  const next = withoutBuiltAppForChat(state, 'chat-a')

  assert.equal(builtAppForChat(next, 'chat-a'), null)
  assert.deepEqual(builtAppForChat(next, 'chat-b'), { id: 8, name: 'Notes' })
})

test('empty chat ids and app ids are ignored', () => {
  const original = { existing: { id: 1, name: 'One' } }

  assert.deepEqual(withBuiltAppForChat(original, null, { id: 2 }), original)
  assert.deepEqual(withBuiltAppForChat(original, 'chat-a', {}), original)
  assert.deepEqual(withoutBuiltAppForChat(original, null), original)
  assert.equal(builtAppForChat(original, null), null)
})
