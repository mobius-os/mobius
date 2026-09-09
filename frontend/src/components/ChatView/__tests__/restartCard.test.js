import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  isRestartCardAction,
  restartCardSelectedOptions,
  restartCardStatusLabel,
} from '../restartCard.js'


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


test('restart cards fail closed for free text, forged ids, and malformed actions', () => {
  assert.equal(restartCardSelectedOptions(action, questions, {
    'Restart to load these changes?': 'yes',
  }), null)
  assert.equal(restartCardSelectedOptions(action, [{
    ...questions[0],
    options: [{ id: 'foreign-id', label: 'Restart now' }],
  }], {
    'Restart to load these changes?': 'Restart now',
  }), null)
  assert.equal(restartCardSelectedOptions({ ...action, version: 2 }, questions, {}), undefined)
})


test('restart status labels distinguish loaded and uncertain outcomes', () => {
  assert.equal(restartCardStatusLabel({ ...action, status: 'activated' }), 'Changes loaded')
  assert.equal(
    restartCardStatusLabel({ ...action, status: 'uncertain' }),
    'Restart outcome needs review',
  )
  assert.equal(
    restartCardStatusLabel({ ...action, status: 'activation_uncertain' }),
    'Restart outcome needs review',
  )
  assert.equal(restartCardStatusLabel(action), '')
})
