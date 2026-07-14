import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  freshChatBuiltApps,
  freshAppIds,
  withAppsFlagged,
  withoutAppFlagged,
} from '../newAppAttention.js'

test('freshAppIds returns only ids absent from the baseline', () => {
  const baseline = new Set([1, 2, 3])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3]), [])
  assert.deepEqual(freshAppIds(baseline, [1, 2, 3, 4]), [4])
  assert.deepEqual(freshAppIds(baseline, [5, 4]), [5, 4])
})

test('freshAppIds normalizes ids so a string route id does not double-count', () => {
  const baseline = new Set([7])
  assert.deepEqual(freshAppIds(baseline, ['7', 8]), [8])
  assert.deepEqual(freshAppIds([7], ['7']), [])
})

test('freshChatBuiltApps returns all fresh chat-owned artifacts in app order', () => {
  const apps = [
    { id: 7, chat_id: 'chat-a' },
    { id: 8, chat_id: null },
    { id: 9, chat_id: 'chat-b' },
  ]
  assert.deepEqual(freshChatBuiltApps(apps, [7, 8, 9]), [
    { appId: 7, chatId: 'chat-a' },
    { appId: 9, chatId: 'chat-b' },
  ])
})

test('freshChatBuiltApps ignores old apps, invalid ids, and store installs', () => {
  const apps = [
    { id: 7, chat_id: 'chat-a' },
    { id: 'bad', chat_id: 'chat-b' },
    { id: 8 },
  ]
  assert.deepEqual(freshChatBuiltApps(apps, [8, 9]), [])
})

test('withAppsFlagged adds ids and keeps the same reference on a no-op', () => {
  const prev = new Set([1])
  const added = withAppsFlagged(prev, [2, 3])
  assert.deepEqual([...added], [1, 2, 3])

  assert.equal(withAppsFlagged(prev, []), prev)
  assert.equal(withAppsFlagged(prev, [1]), prev)
})

test('withoutAppFlagged clears one id and no-ops when absent', () => {
  const prev = new Set([1, 2])
  const cleared = withoutAppFlagged(prev, 2)
  assert.deepEqual([...cleared], [1])

  assert.equal(withoutAppFlagged(prev, 9), prev)
  assert.equal(withoutAppFlagged(prev, '2') === prev, false)
  assert.deepEqual([...withoutAppFlagged(prev, '2')], [1])
})
