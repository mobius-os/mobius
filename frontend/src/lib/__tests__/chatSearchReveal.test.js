import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  CHAT_SEARCH_REVEAL_TTL_MS,
  chatSearchRevealFor,
  clearChatSearchReveal,
  consumeChatSearchActivation,
  reconcileChatSearchActivation,
  requestChatSearchReveal,
  subscribeChatSearchReveal,
} from '../chatSearchReveal.js'

test('search reveal is transient and only its matching request may clear it', () => {
  const first = requestChatSearchReveal('chat-1', {
    anchorKey: 'assistant-12', terms: ['wombat'],
  })
  const second = requestChatSearchReveal('chat-1', {
    anchorKey: 'user-13', terms: ['capybara'],
  })
  assert.equal(clearChatSearchReveal('chat-1', first.id), false)
  assert.equal(chatSearchRevealFor('chat-1').anchorKey, 'user-13')
  assert.equal(clearChatSearchReveal('chat-1', second.id), true)
  assert.equal(chatSearchRevealFor('chat-1'), null)

  let activation = reconcileChatSearchActivation(null, 'chat-1', second)
  activation = consumeChatSearchActivation(activation, second.id)
  activation = reconcileChatSearchActivation(activation, 'chat-1', null)
  assert.equal(activation.reveal.id, second.id,
    'consumption keeps this activation on the searched row after store cleanup')
  activation = reconcileChatSearchActivation(activation, 'chat-2', null)
  assert.equal(activation.reveal, null, 'a chat change clears the captured activation')
})

test('mounted chat listeners receive a same-chat search intent', () => {
  let notices = 0
  const stop = subscribeChatSearchReveal('chat-mounted', () => { notices += 1 })
  const reveal = requestChatSearchReveal('chat-mounted', {
    anchorKey: 'user-7', terms: ['needle'],
  })
  assert.equal(notices, 1)
  clearChatSearchReveal('chat-mounted', reveal.id)
  assert.equal(notices, 2)
  stop()
})

test('expiry notifies a mounted chat and an older timer cannot clear its replacement', () => {
  const originalSetTimeout = globalThis.setTimeout
  const originalClearTimeout = globalThis.clearTimeout
  const scheduled = []
  globalThis.setTimeout = callback => {
    scheduled.push(callback)
    return scheduled.length
  }
  globalThis.clearTimeout = () => {}
  let notices = 0
  const stop = subscribeChatSearchReveal('chat-expired', () => { notices += 1 })
  try {
    const first = requestChatSearchReveal('chat-expired', {
      anchorKey: 'assistant-2', terms: ['old'],
    })
    const second = requestChatSearchReveal('chat-expired', {
      anchorKey: 'assistant-3', terms: ['new'],
    })
    assert.ok(first.expiresAt - Date.now() <= CHAT_SEARCH_REVEAL_TTL_MS)
    assert.ok(second.expiresAt >= first.expiresAt)
    assert.equal(notices, 2)
    scheduled[0]()
    assert.equal(chatSearchRevealFor('chat-expired').id, second.id)
    assert.equal(notices, 2)
    scheduled[1]()
    assert.equal(chatSearchRevealFor('chat-expired'), null)
    assert.equal(notices, 3)
  } finally {
    stop()
    clearChatSearchReveal('chat-expired')
    globalThis.setTimeout = originalSetTimeout
    globalThis.clearTimeout = originalClearTimeout
  }
})
