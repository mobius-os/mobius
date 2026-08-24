import test from 'node:test'
import assert from 'node:assert/strict'

import {
  questionResponseActivityChanged,
  questionResponseActivitySnapshot,
  questionResponseBaselineSnapshot,
} from '../questionResponseActivity.js'

const question = {
  type: 'question',
  question_id: 'q-1',
  questions: [{ question: 'Continue?' }],
  absorbedTool: 'AskUserQuestion',
  absorbedToolUseId: 'tool-1',
}

test('answer controls and raw question-tool settlement are not response activity', () => {
  const snapshot = questionResponseActivitySnapshot([
    { type: 'text', content: 'Before' },
    question,
  ])
  assert.equal(questionResponseActivityChanged(snapshot, [
    { type: 'text', content: 'Before' },
    { ...question, answers: { Continue: 'Yes' } },
  ]), false)
  assert.equal(questionResponseActivityChanged(snapshot, [
    { type: 'text', content: 'Before' },
    {
      type: 'question',
      question_id: 'q-1',
      questions: [{ question: 'Continue?' }],
      answers: { Continue: 'Yes' },
    },
  ]), false)
})

test('text, thinking, tool, and error changes are response activity', () => {
  const items = [{ type: 'text', content: 'Before' }, question]
  const snapshot = questionResponseActivitySnapshot(items)
  for (const activity of [
    { type: 'text', content: 'After' },
    { type: 'thinking', content: 'Working' },
    { type: 'tool', tool: 'Bash', status: 'running' },
    { type: 'error', message: 'Stopped' },
  ]) {
    assert.equal(questionResponseActivityChanged(
      snapshot,
      [...items, activity],
    ), true)
  }
})

test('catch-up object key order cannot manufacture response activity', () => {
  const snapshot = questionResponseActivitySnapshot([
    { type: 'text', content: 'Before', source: { url: 'u', title: 't' } },
    question,
  ])
  assert.equal(questionResponseActivityChanged(snapshot, [
    { source: { title: 't', url: 'u' }, content: 'Before', type: 'text' },
    {
      questions: [{ question: 'Continue?' }],
      question_id: 'q-1',
      type: 'question',
      answers: { Continue: 'Yes' },
    },
  ]), false)
})

test('a catch-up snapshot containing post-answer content is response activity', () => {
  const snapshot = questionResponseActivitySnapshot([
    { type: 'text', content: 'Before' },
    question,
  ])
  assert.equal(questionResponseActivityChanged(snapshot, [
    { type: 'text', content: 'Before' },
    { ...question, answers: { Continue: 'Yes' } },
    { type: 'text', content: 'After reconnect' },
  ]), true)
})

test('arming baseline detects a continuation already present at arm time', () => {
  // A reconnect delivers the answered question AND its post-answer text in one
  // snapshot, so arming captures its baseline from items that already carry the
  // continuation. Keying the baseline to the answered question keeps that
  // continuation detectable; a whole-surface baseline would swallow it and the
  // handoff would never fire.
  const armItems = [
    { type: 'text', content: 'Before' },
    { ...question, answers: { Continue: 'Yes' } },
    { type: 'text', content: 'After reconnect' },
  ]
  const baseline = questionResponseBaselineSnapshot(armItems, 'question_id:q-1')
  assert.equal(questionResponseActivityChanged(baseline, armItems), true)
})

test('arming baseline holds until real continuation renders', () => {
  // Live path: the answered question is the last item at arm time, so the
  // baseline equals the current surface and reports no activity until content
  // actually lands after it.
  const armItems = [
    { type: 'text', content: 'Before' },
    { ...question, answers: { Continue: 'Yes' } },
  ]
  const baseline = questionResponseBaselineSnapshot(armItems, 'question_id:q-1')
  assert.equal(questionResponseActivityChanged(baseline, armItems), false)
  assert.equal(questionResponseActivityChanged(
    baseline,
    [...armItems, { type: 'text', content: 'Continuation' }],
  ), true)
})

test('arming baseline keys on the answered card among several', () => {
  const later = { ...question, question_id: 'q-2' }
  const armItems = [
    question,
    { type: 'text', content: 'between' },
    { ...later, answers: { Continue: 'Yes' } },
    { type: 'text', content: 'after q-2' },
  ]
  const baseline = questionResponseBaselineSnapshot(armItems, 'question_id:q-2')
  assert.equal(questionResponseActivityChanged(baseline, armItems), true)
})
