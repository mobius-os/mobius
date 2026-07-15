import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  currentReusableEmptyChat,
  detailIsUntouchedEmptyChat,
} from '../newChatPolicy.js'

const empty = (id, extra = {}) => ({
  id,
  has_messages: false,
  running: false,
  run_status: null,
  ...extra,
})

test('only the active empty chat is eligible for client-side reuse', () => {
  const offscreen = empty('offscreen')
  const active = empty('active')

  assert.equal(currentReusableEmptyChat([offscreen, active], {
    activeChatId: 'active',
  }), active)
  assert.equal(currentReusableEmptyChat([offscreen], {
    activeChatId: 'active',
  }), null)
})

test('drafts and force-new callers always require a fresh chat', () => {
  const active = empty('active')
  assert.equal(currentReusableEmptyChat([active], {
    activeChatId: 'active', draft: true,
  }), null)
  assert.equal(currentReusableEmptyChat([active], {
    activeChatId: 'active', forceNew: true,
  }), null)
})

test('running, excluded, recovered, and populated active chats are rejected', () => {
  const options = { activeChatId: 'active' }
  assert.equal(currentReusableEmptyChat([empty('active', { running: true })], options), null)
  assert.equal(currentReusableEmptyChat([empty('active', { run_status: 'running' })], options), null)
  assert.equal(currentReusableEmptyChat([empty('active', { has_messages: true })], options), null)
  assert.equal(currentReusableEmptyChat([empty('active')], {
    ...options, exclude: 'active',
  }), null)
  assert.equal(currentReusableEmptyChat([empty('active')], {
    ...options, recoveredChatIds: new Set(['active']),
  }), null)
  assert.equal(currentReusableEmptyChat([empty('active')], {
    ...options, streamingChatIds: new Set(['active']),
  }), null)
})

test('id comparison is stable across numeric and string representations', () => {
  const active = empty(7)
  assert.equal(currentReusableEmptyChat([active], {
    activeChatId: '7',
  }), active)
})

function untouchedDetail(extra = {}) {
  return {
    total: 0,
    messages: [],
    pending_messages: [],
    running: false,
    pending_question_id: null,
    session_id: null,
    ...extra,
  }
}

test('fresh detail accepts only a fully untouched empty chat', () => {
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail()), true)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ total: 1 })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ messages: [{}] })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ pending_messages: [{}] })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ running: true })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ pending_question_id: 'q' })), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ session_id: 'session' })), false)
})

test('fresh detail fails closed on partial or malformed responses', () => {
  assert.equal(detailIsUntouchedEmptyChat(null), false)
  assert.equal(detailIsUntouchedEmptyChat({ messages: [], pending_messages: [] }), false)
  assert.equal(detailIsUntouchedEmptyChat(untouchedDetail({ total: '0' })), false)
})
