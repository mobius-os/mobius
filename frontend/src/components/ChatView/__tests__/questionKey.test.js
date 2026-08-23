import test from 'node:test'
import assert from 'node:assert/strict'

import { lastQuestionKey, lastQuestionIndex } from '../questionKey.js'

test('lastQuestionKey selects the last question item', () => {
  const items = [
    { type: 'question', question_id: 'a' },
    { type: 'text', content: 'x' },
    { type: 'question', question_id: 'b' },
    { type: 'text', content: 'y' },
  ]
  assert.equal(lastQuestionKey(items), 'question_id:b')
})

test('lastQuestionKey is null without a question item', () => {
  assert.equal(lastQuestionKey([{ type: 'text' }]), null)
  assert.equal(lastQuestionKey([]), null)
  assert.equal(lastQuestionKey(null), null)
})

test('lastQuestionIndex can constrain to one key', () => {
  const items = [
    { type: 'question', question_id: 'a' },
    { type: 'question', question_id: 'b' },
    { type: 'text' },
  ]
  assert.equal(lastQuestionIndex(items), 1)
  assert.equal(lastQuestionIndex(items, 'question_id:a'), 0)
  assert.equal(lastQuestionIndex(items, 'question_id:missing'), -1)
})
