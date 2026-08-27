import assert from 'node:assert/strict'
import test from 'node:test'

import { submitFailure } from '../contributionReviewModel.js'

const READY = { id: 'r1', status: 'prepared' }

test('a failed send still explains itself after the card is reloaded', () => {
  const failure = submitFailure({
    ...READY,
    last_submit_error: 'This did not pass the checks Möbius runs before publishing.',
    last_submit_error_detail: '[pre-push] frontend-unit FAILED:',
  })

  assert.deepEqual(failure, {
    message: 'This did not pass the checks Möbius runs before publishing.',
    detail: '[pre-push] frontend-unit FAILED:',
  })
})

test('a new in-flight attempt suppresses a stale durable failure', () => {
  const record = {
    ...READY,
    last_submit_error: 'durable reason',
    last_submit_error_detail: 'durable detail',
  }
  assert.equal(
    submitFailure(record, { attempt: { message: 'local reason' }, sending: true }),
    null,
  )
})

test('the current attempt wins until the ledger refresh arrives', () => {
  assert.deepEqual(
    submitFailure(READY, {
      attempt: { message: 'current reason', detail: 'current detail' },
    }),
    { message: 'current reason', detail: 'current detail' },
  )
})

test('a record that never failed shows nothing', () => {
  assert.equal(submitFailure(READY), null)
  assert.equal(submitFailure({ ...READY, last_submit_error: '   ' }), null)
  assert.equal(submitFailure(null), null)
})

test('a message without a transcript is still a complete failure', () => {
  const failure = submitFailure({
    ...READY,
    last_submit_error: 'Could not reach the server. Nothing was contributed.',
  })

  assert.deepEqual(failure, {
    message: 'Could not reach the server. Nothing was contributed.',
    detail: '',
  })
})

test('a detail with no message is not a failure the card can explain', () => {
  assert.equal(
    submitFailure({ ...READY, last_submit_error_detail: 'orphaned transcript' }),
    null,
  )
})
