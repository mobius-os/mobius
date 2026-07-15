import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  clearChatQuestionDrafts,
  clearQuestionDraft,
  questionDraftKey,
  readQuestionDraft,
  writeQuestionDraft,
} from '../questionDraft.js'


class MemoryStorage {
  constructor() { this.values = new Map() }
  get length() { return this.values.size }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(key, String(value)) }
  removeItem(key) { this.values.delete(key) }
  key(index) { return [...this.values.keys()][index] ?? null }
}


const questions = [{
  question: 'Which direction?',
  multiSelect: true,
  options: [{ label: 'Polish' }, { label: 'Simplify' }],
}]


test('question drafts restore selections and custom text by chat and question', () => {
  const storage = new MemoryStorage()
  const key = questionDraftKey('chat-1', 'question-7', questions)

  writeQuestionDraft(
    key,
    { 'Which direction?': ['Polish', '__other__'] },
    { 'Which direction?': 'Keep the current shape' },
    storage,
  )

  assert.deepEqual(readQuestionDraft(key, storage), {
    answers: { 'Which direction?': ['Polish', '__other__'] },
    otherTexts: { 'Which direction?': 'Keep the current shape' },
  })
  assert.deepEqual(
    readQuestionDraft(questionDraftKey('chat-2', 'question-7', questions), storage),
    { answers: {}, otherTexts: {} },
    'another chat must not inherit the selection',
  )
})


test('legacy questions without an id get a stable content-derived draft key', () => {
  const first = questionDraftKey('chat-1', null, questions)
  const second = questionDraftKey('chat-1', null, structuredClone(questions))
  assert.equal(first, second)
  assert.notEqual(first, questionDraftKey('chat-1', null, [{ question: 'Different?' }]))
})


test('question drafts clear on submit and with the owning chat', () => {
  const storage = new MemoryStorage()
  const first = questionDraftKey('chat-1', 'q1', questions)
  const second = questionDraftKey('chat-1', 'q2', questions)
  const otherChat = questionDraftKey('chat-2', 'q1', questions)
  for (const key of [first, second, otherChat]) {
    writeQuestionDraft(key, { 'Which direction?': 'Polish' }, {}, storage)
  }

  clearQuestionDraft(first, storage)
  assert.equal(storage.getItem(first), null)
  clearChatQuestionDrafts('chat-1', storage)
  assert.equal(storage.getItem(second), null)
  assert.notEqual(storage.getItem(otherChat), null)
})
