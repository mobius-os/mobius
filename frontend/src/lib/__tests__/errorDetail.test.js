import assert from 'node:assert/strict'
import test from 'node:test'

import { detailToMessage } from '../errorDetail.js'

test('passes a plain string detail through, trimmed', () => {
  assert.equal(detailToMessage('  App chats cannot change provider.  '), 'App chats cannot change provider.')
})

test('empty/whitespace string falls back', () => {
  assert.equal(detailToMessage('', 'fallback'), 'fallback')
  assert.equal(detailToMessage('   ', 'fallback'), 'fallback')
})

test('a Pydantic 422 validation array becomes the joined msg text — never an object', () => {
  // This is the exact shape FastAPI returns when ChatProviderSwitch's
  // after-validator rejects an incompatible target effort. Rendering the raw
  // array as a React child is what caused React error #31 and crashed the shell.
  const detail = [
    {
      type: 'value_error',
      loc: ['body'],
      msg: 'Value error, target effort does not belong to target provider',
      input: { provider: 'codex' },
      ctx: { error: {} },
    },
  ]
  const result = detailToMessage(detail, 'fallback')
  assert.equal(typeof result, 'string')
  assert.equal(result, 'Value error, target effort does not belong to target provider')
})

test('joins multiple validation entries', () => {
  const detail = [
    { type: 'value_error', loc: ['body', 'a'], msg: 'first problem' },
    { type: 'value_error', loc: ['body', 'b'], msg: 'second problem' },
  ]
  assert.equal(detailToMessage(detail), 'first problem; second problem')
})

test('validation array with no usable msg falls back', () => {
  assert.equal(detailToMessage([{ type: 'x', loc: ['body'] }], 'fallback'), 'fallback')
  assert.equal(detailToMessage([], 'fallback'), 'fallback')
})

test('single validation-entry object surfaces its msg', () => {
  assert.equal(
    detailToMessage({ type: 'value_error', loc: ['body'], msg: 'bad target model' }),
    'bad target model',
  )
})

test('nested error envelopes resolve to a string', () => {
  assert.equal(detailToMessage({ message: 'nope' }), 'nope')
  assert.equal(detailToMessage({ detail: 'wrapped string' }), 'wrapped string')
  assert.equal(detailToMessage({ detail: [{ msg: 'nested msg' }] }), 'nested msg')
})

test('null/undefined/number detail fall back to the provided default', () => {
  assert.equal(detailToMessage(null, 'fallback'), 'fallback')
  assert.equal(detailToMessage(undefined, 'fallback'), 'fallback')
  assert.equal(detailToMessage(42, 'fallback'), 'fallback')
})

test('default fallback is an empty string, guaranteeing a renderable value', () => {
  assert.equal(detailToMessage(null), '')
  assert.equal(detailToMessage([{ type: 'x' }]), '')
})
