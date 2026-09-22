import assert from 'node:assert/strict'
import test from 'node:test'

import {
  chatQuestionRevealFor,
  consumeChatQuestionReveal,
  requestChatQuestionReveal,
  subscribeChatQuestionReveal,
} from '../chatQuestionReveal.js'

test('a notification reveal is local to its chat and is consumed exactly once', () => {
  let notices = 0
  const unsubscribe = subscribeChatQuestionReveal('chat-answer', () => { notices += 1 })
  const request = requestChatQuestionReveal('chat-answer')
  assert.ok(request?.id)
  assert.equal(chatQuestionRevealFor('chat-answer')?.id, request.id)
  assert.equal(chatQuestionRevealFor('other-chat'), null)
  assert.equal(consumeChatQuestionReveal('chat-answer', request.id), true)
  assert.equal(chatQuestionRevealFor('chat-answer'), null)
  assert.equal(consumeChatQuestionReveal('chat-answer', request.id), false)
  assert.equal(notices, 2)
  unsubscribe()
})
