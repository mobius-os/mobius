import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import {
  isRestartCardAction,
  restartCardSelectedOptions,
  restartCardStatusDetail,
  restartCardStatusLabel,
} from '../restartCard.js'
import QuestionCard from '../QuestionCard.jsx'


const action = {
  type: 'restart',
  version: 1,
  status: 'awaiting_owner',
  restart_option_id: 'restart-id',
  cancel_option_id: 'cancel-id',
}
const questions = [{
  id: 'restart',
  question: 'Restart to load these changes?',
  options: [
    { id: 'cancel-id', label: 'Not now' },
    { id: 'restart-id', label: 'Restart now' },
  ],
}]


test('restart cards submit only the server-issued id for the displayed choice', () => {
  assert.equal(isRestartCardAction(action), true)
  assert.deepEqual(restartCardSelectedOptions(action, questions, {
    'Restart to load these changes?': 'Restart now',
  }), { restart: ['restart-id'] })
  assert.deepEqual(restartCardSelectedOptions(action, questions, {
    'Restart to load these changes?': 'Not now',
  }), { restart: ['cancel-id'] })
})


test('restart cards separate written feedback from forged action ids', () => {
  assert.deepEqual(restartCardSelectedOptions(action, questions, {
    'Restart to load these changes?': 'Please check the tests first',
  }), {})
  assert.equal(restartCardSelectedOptions(action, [{
    ...questions[0],
    options: [{ id: 'foreign-id', label: 'Restart now' }],
  }], {
    'Restart to load these changes?': 'Restart now',
  }), null)
  assert.equal(restartCardSelectedOptions({
    ...action, version: 2, restart_option_id: '', cancel_option_id: 'cancel-id',
  }, questions, {
    'Restart to load these changes?': 'Not now',
  }), null)
  assert.equal(isRestartCardAction({ ...action, version: 2 }), true)
  assert.equal(isRestartCardAction({ ...action, version: 3 }), false)
})


test('restart status labels distinguish observed and uncertain outcomes', () => {
  assert.equal(restartCardStatusLabel({ ...action, status: 'activated' }), 'Möbius restarted')
  assert.match(
    restartCardStatusDetail({ ...action, status: 'activated' }),
    /agent will check whether these changes loaded/,
  )
  assert.equal(
    restartCardStatusLabel({ ...action, status: 'uncertain' }),
    'Restart outcome needs review',
  )
  assert.equal(
    restartCardStatusLabel({ ...action, status: 'activation_uncertain' }),
    'Restart outcome needs review',
  )
  assert.equal(restartCardStatusLabel(action), '')
  assert.match(
    restartCardStatusDetail({ ...action, version: 2, status: 'expired' }),
    /Nothing was restarted/,
  )
})


test('pending Restart card offers one exact action and a written response', () => {
  const current = {
    ...action,
    version: 2,
    cancel_option_id: undefined,
  }
  const html = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'chat', questionId: 'question', platformAction: current,
    questions: [{
      id: 'restart', question: 'Restart to load these changes?', options: [
        { id: 'restart-id', label: 'Restart now', description: 'Load changes.' },
      ],
    }],
    disabled: false,
  }))

  assert.match(html, />Restart now</)
  assert.match(html, /Or tell me what you’d like to do instead…/)
  assert.match(html, />Continue</)
  assert.doesNotMatch(html, /Not now/)
})


test('closed Restart card explains the outcome without dead controls', () => {
  const html = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'chat', questionId: 'question',
    platformAction: { ...action, version: 2, status: 'expired' },
    questions,
    disabled: true,
  }))

  assert.match(html, /Restart request closed/)
  assert.match(html, /Nothing was restarted/)
  assert.doesNotMatch(html, /textarea/)
  assert.doesNotMatch(html, /qcard__opt/)
})
