/* One "still asking" check for a card's controls, its answerability and Copy. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { questionActionCompleted, questionCardOpen } from '../questionOpen.js'

const card = { type: 'question', question_id: 'q1', questions: [{ question: 'Ship it?' }] }
const restart = status => ({ type: 'restart', version: 2, status })

test('a card is open only while it is the chat’s open question and unanswered', () => {
  assert.equal(questionCardOpen(card, 'q1'), true)
  assert.equal(questionCardOpen({ ...card, answers: { 'Ship it?': 'Yes' } }, 'q1'), false, 'answered')
  assert.equal(questionCardOpen(card, null), false, 'stopped: the chat has no open question')
  assert.equal(questionCardOpen(card, 'q2'), false, 'superseded by a later question')
  assert.equal(questionCardOpen({ type: 'text', question_id: 'q1' }, 'q1'), false)
})

test('a restart card stays open while it awaits the owner and closes once acted on', () => {
  assert.equal(questionCardOpen({ ...card, platform_action: restart('awaiting_owner') }, null), true,
    'a durable Restart offer stays actionable until pressed')
  for (const status of ['restart_requested', 'activated', 'responded', 'expired', 'failed']) {
    assert.equal(questionActionCompleted(restart(status)), true, status)
    assert.equal(questionCardOpen({ ...card, platform_action: restart(status) }, 'q1'), false, status)
  }
  assert.equal(questionActionCompleted(restart('awaiting_owner')), false)
  assert.equal(questionActionCompleted(undefined), false)
})
