import test from 'node:test'
import assert from 'node:assert/strict'

import {
  acknowledgeChatFailure,
  failedChatIds,
  withChatFailureSeen,
} from '../chatFailureAttention.js'


test('only hidden chats with durable unseen failures receive drawer attention', () => {
  const chats = [
    { id: 'hidden-failed', has_unseen_failure: true, unseen_failure_version: 2 },
    { id: 'visible-failed', has_unseen_failure: true, unseen_failure_version: 1 },
    { id: 'healthy', has_unseen_failure: false, unseen_failure_version: null },
  ]
  assert.deepEqual(
    [...failedChatIds(chats, new Set(['visible-failed']))],
    ['hidden-failed'],
  )
})


test('optimistic acknowledgement cannot hide a newer cached failure', () => {
  const chats = [{
    id: 'chat-1', has_unseen_failure: true, unseen_failure_version: 3,
  }]
  assert.equal(withChatFailureSeen(chats, 'chat-1', 2), chats)
  assert.deepEqual(withChatFailureSeen(chats, 'chat-1', 3), [{
    id: 'chat-1', has_unseen_failure: false, unseen_failure_version: null,
  }])
})


test('acknowledgement deduplicates requests and restores server truth on failure', async () => {
  const inFlight = new Set()
  const cleared = []
  let restoreCount = 0
  let release
  const response = new Promise(resolve => { release = resolve })
  const first = acknowledgeChatFailure({
    chatId: 'chat-2',
    activityVersion: 4,
    inFlight,
    request: () => response,
    clearCached: (...args) => cleared.push(args),
    restoreServerTruth: async () => { restoreCount += 1 },
  })
  const duplicate = await acknowledgeChatFailure({
    chatId: 'chat-2',
    activityVersion: 4,
    inFlight,
    request: () => assert.fail('duplicate request ran'),
    clearCached: () => assert.fail('duplicate clear ran'),
    restoreServerTruth: () => assert.fail('duplicate restore ran'),
  })
  assert.equal(duplicate, false)
  release({ ok: false, status: 503 })
  assert.equal(await first, false)
  assert.deepEqual(cleared, [['chat-2', 4]])
  assert.equal(restoreCount, 1)
  assert.equal(inFlight.size, 0)
})


test('successful acknowledgement clears again after the request settles', async () => {
  const cleared = []
  const result = await acknowledgeChatFailure({
    chatId: 'chat-3',
    activityVersion: 7,
    inFlight: new Set(),
    request: async () => ({ ok: true }),
    clearCached: (...args) => cleared.push(args),
    restoreServerTruth: () => assert.fail('success must not restore'),
  })
  assert.equal(result, true)
  assert.deepEqual(cleared, [['chat-3', 7], ['chat-3', 7]])
})
