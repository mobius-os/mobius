import assert from 'node:assert/strict'
import test from 'node:test'

import {
  chatQuestionRevealFor,
  consumeChatQuestionReveal,
  markQuestionArrivalRevealed,
  questionArrivalRevealed,
  requestChatQuestionReveal,
  subscribeChatQuestionReveal,
} from '../chatQuestionReveal.js'

test('a settled decision arrival is never replayed for the same chat and question', () => {
  assert.equal(questionArrivalRevealed('chat-arrival', 'q-1'), false)
  markQuestionArrivalRevealed('chat-arrival', 'q-1')
  assert.equal(questionArrivalRevealed('chat-arrival', 'q-1'), true)
  // A later visit to the same waiting decision must not pull the reader back.
  assert.equal(questionArrivalRevealed('chat-arrival', 'q-1'), true)
})

test('a newer decision in the same chat reveals itself again', () => {
  markQuestionArrivalRevealed('chat-next-decision', 'q-old')
  assert.equal(questionArrivalRevealed('chat-next-decision', 'q-new'), false)
  markQuestionArrivalRevealed('chat-next-decision', 'q-new')
  assert.equal(questionArrivalRevealed('chat-next-decision', 'q-new'), true)
})

test('decision arrival memory never crosses chats or accepts an unnamed question', () => {
  markQuestionArrivalRevealed('chat-a', 'shared-question-id')
  assert.equal(questionArrivalRevealed('chat-b', 'shared-question-id'), false)
  markQuestionArrivalRevealed('chat-c', null)
  assert.equal(questionArrivalRevealed('chat-c', null), false)
  assert.equal(questionArrivalRevealed('', 'shared-question-id'), false)
})

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
