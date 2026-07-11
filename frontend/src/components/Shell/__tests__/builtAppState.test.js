import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  builtAppsForChat,
  coerceBuiltAppsByChat,
  withBuiltAppForChat,
  withoutBuiltAppForChat,
} from '../builtAppState.js'

test('built apps are scoped to the chat that produced them', () => {
  const state = withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Habits' })

  assert.deepEqual(builtAppsForChat(state, 'chat-a'), [{ id: 7, name: 'Habits' }])
  assert.deepEqual(builtAppsForChat(state, 'chat-b'), [])
})

test('a chat can hold several built apps, most recent last', () => {
  const state = withBuiltAppForChat(
    withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Notes' }),
    'chat-a',
    { id: 8, name: 'Habits' },
  )

  assert.deepEqual(builtAppsForChat(state, 'chat-a'), [
    { id: 7, name: 'Notes' },
    { id: 8, name: 'Habits' },
  ])
})

test('rebuilding an app dedups by id and moves it to the end', () => {
  const state = withBuiltAppForChat(
    withBuiltAppForChat(
      withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Notes' }),
      'chat-a',
      { id: 8, name: 'Habits' },
    ),
    'chat-a',
    { id: 7, name: 'Notes v2' },
  )

  assert.deepEqual(builtAppsForChat(state, 'chat-a'), [
    { id: 8, name: 'Habits' },
    { id: 7, name: 'Notes v2' },
  ])
})

test('only the newest three built apps are kept', () => {
  let state = {}
  for (const app of [
    { id: 1, name: 'A' }, { id: 2, name: 'B' },
    { id: 3, name: 'C' }, { id: 4, name: 'D' },
  ]) {
    state = withBuiltAppForChat(state, 'chat-a', app)
  }

  assert.deepEqual(builtAppsForChat(state, 'chat-a').map(a => a.id), [2, 3, 4])
})

test('clearing one chat does not clear another chat preview', () => {
  const state = withBuiltAppForChat(
    withBuiltAppForChat({}, 'chat-a', { id: 7, name: 'Habits' }),
    'chat-b',
    { id: 8, name: 'Notes' },
  )

  const next = withoutBuiltAppForChat(state, 'chat-a')

  assert.deepEqual(builtAppsForChat(next, 'chat-a'), [])
  assert.deepEqual(builtAppsForChat(next, 'chat-b'), [{ id: 8, name: 'Notes' }])
})

test('empty chat ids and app ids are ignored', () => {
  const original = { existing: [{ id: 1, name: 'One' }] }

  assert.deepEqual(withBuiltAppForChat(original, null, { id: 2 }), original)
  assert.deepEqual(withBuiltAppForChat(original, 'chat-a', {}), original)
  assert.deepEqual(withoutBuiltAppForChat(original, null), original)
  assert.deepEqual(builtAppsForChat(original, null), [])
})

test('an empty chat always yields the same list reference', () => {
  // ChatView relies on this stable identity so its list-keyed effects do
  // not fire on every render for chats that built nothing.
  assert.equal(builtAppsForChat({}, 'chat-a'), builtAppsForChat({}, 'chat-b'))
})

test('coerceBuiltAppsByChat restores the list shape from persistence', () => {
  assert.deepEqual(
    coerceBuiltAppsByChat({ 'chat-a': [{ id: 7, name: 'Habits' }] }),
    { 'chat-a': [{ id: 7, name: 'Habits' }] },
  )
})

test('coerceBuiltAppsByChat tolerates a legacy one-app scalar', () => {
  assert.deepEqual(
    coerceBuiltAppsByChat({ 'chat-a': { id: 7, name: 'Habits' } }),
    { 'chat-a': [{ id: 7, name: 'Habits' }] },
  )
})

test('coerceBuiltAppsByChat drops malformed entries and non-objects', () => {
  assert.deepEqual(coerceBuiltAppsByChat(null), {})
  assert.deepEqual(coerceBuiltAppsByChat('nope'), {})
  assert.deepEqual(
    coerceBuiltAppsByChat({
      'chat-a': [{ id: 1 }, { name: 'no id' }, null],
      'chat-b': [],
      'chat-c': { name: 'no id' },
    }),
    { 'chat-a': [{ id: 1 }] },
  )
})
