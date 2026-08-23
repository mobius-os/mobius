import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  failedNewChatPresentation,
  shouldRetryNewChatAllocation,
  stageVerifiedNewChatHandoff,
} from '../newChatPolicy.js'

test('a provisional Send is durable before recovery can retry its allocation', () => {
  const writes = []
  const handoffs = new Map()
  const stageHandoff = (chatId, text, options) => {
    writes.push([chatId, text, options])
    handoffs.set(chatId, { autoSendDraft: text })
  }
  const readHandoff = chatId => handoffs.get(chatId) || {}

  assert.equal(stageVerifiedNewChatHandoff('chat-1', 'hello', {
    stageHandoff,
    readHandoff,
  }), true)
  assert.deepEqual(writes, [['chat-1', 'hello', { autoSend: true }]])
  assert.equal(stageVerifiedNewChatHandoff('chat-2', 'hello', {
    stageHandoff: () => {},
    readHandoff,
  }), false, 'a missing readback must not be presented as queued')
  assert.equal(stageVerifiedNewChatHandoff('chat-3', '   ', {
    stageHandoff,
    readHandoff,
  }), false)

  const failed = failedNewChatPresentation({ submitted: true, token: 7 }, 'offline', 3)
  assert.deepEqual(failed, {
    submitted: true,
    token: 7,
    failure: 'offline',
    failedAtRecoveryGeneration: 3,
  }, 'a late failure preserves the queued snapshot')
  assert.equal(shouldRetryNewChatAllocation(failed, 3), false)
  assert.equal(shouldRetryNewChatAllocation(failed, 4), true)
  assert.equal(shouldRetryNewChatAllocation({ ...failed, failure: 'queue' }, 4), false)
  assert.equal(shouldRetryNewChatAllocation({ ...failed, materialized: true }, 4), false)
})
