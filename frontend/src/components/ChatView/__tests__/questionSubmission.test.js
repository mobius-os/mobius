// Explicit option provenance controls presentation; free text never gains action authority.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { questionOptionSubmission, questionAnswerPatch } from '../questionSubmission.js'

const question = {
  id: 'help', question: 'Anything else?', options: [
    { id: '0', label: 'No', on_answer: 'close' },
    { id: '1', label: 'Yes' },
  ],
}

test('actual quiet option selection carries immutable ids and a quiet presentation hint', () => {
  assert.deepEqual(questionOptionSubmission([question], { 'Anything else?': 'No' }), {
    selected_options: { help: ['0'] }, closeOnlySelection: true,
  })
})

test('custom text equal to an option label never acquires its identity', () => {
  // QuestionCard stores custom input separately; answer state is the sentinel,
  // even when otherTexts['Anything else?'] happens to contain "No".
  assert.deepEqual(questionOptionSubmission([question], { 'Anything else?': '__other__' }), {
    selected_options: {}, closeOnlySelection: false,
  })
})

test('mixed custom and selected multi-answer omits the whole subquestion option claim', () => {
  assert.deepEqual(questionOptionSubmission([{ ...question, multiSelect: true }], {
    'Anything else?': ['No', '__other__'],
  }), { selected_options: {}, closeOnlySelection: false })
})

test('default and legacy options remain normal agent answers', () => {
  assert.deepEqual(questionOptionSubmission([question], { 'Anything else?': 'Yes' }), {
    selected_options: { help: ['1'] }, closeOnlySelection: false,
  })
  assert.deepEqual(questionOptionSubmission([{
    question: 'Anything else?', options: [{ label: 'No' }],
  }], { 'Anything else?': 'No' }), { selected_options: {}, closeOnlySelection: false })
})

test('every grouped question must be an explicit close selection', () => {
  const second = { id: 'second', question: 'Ready?', options: [{ id: '0', label: 'Done', on_answer: 'close' }] }
  assert.equal(questionOptionSubmission([question, second], { 'Anything else?': 'No' }).closeOnlySelection, false)
  assert.deepEqual(questionOptionSubmission([question, second], {
    'Anything else?': 'No', 'Ready?': 'Done',
  }), { selected_options: { help: ['0'], second: ['0'] }, closeOnlySelection: true })
  assert.equal(questionOptionSubmission([], {}).closeOnlySelection, false)
})


test('unidentified selections omit their entire subquestion without dropping other explicit questions', () => {
  const second = { id: 'second', question: 'Ready?', options: [{ id: '0', label: 'Done', on_answer: 'close' }] }
  for (const unknown of ['__other__', 'Removed choice']) {
    assert.deepEqual(questionOptionSubmission([question, second], {
      'Anything else?': ['No', unknown], 'Ready?': 'Done',
    }), { selected_options: { second: ['0'] }, closeOnlySelection: false })
  }
})


test('answer receipts retain exact action state and option identity across replay', () => {
  const action = { type: 'restart', version: 1, status: 'deferred' }
  assert.deepEqual(questionAnswerPatch({ 'Restart?': 'Not now' }, {
    answer_turn: 'none', selected_options: { restart: ['cancel-id'] }, platform_action: action,
    running: false,
  }), { answers: { 'Restart?': 'Not now' }, answer_turn: 'none',
    selected_options: { restart: ['cancel-id'] }, platform_action: action })
})
