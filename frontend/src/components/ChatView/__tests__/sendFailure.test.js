import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isModelSelectionRequiredFailure,
  isPendingQuestionSendFailure,
  sendFailureMessage,
} from '../sendFailure.js'
import {
  ChatHttpError,
  ChatTransportError,
  chatHttpError,
} from '../sendErrors.js'

test('send failures distinguish connection, timeout, service, and generic errors', () => {
  assert.match(
    sendFailureMessage(new ChatTransportError(new TypeError('Failed to fetch'))),
    /couldn’t confirm the send/,
  )

  const timeout = new Error('aborted')
  timeout.name = 'AbortError'
  assert.match(sendFailureMessage(timeout), /too long/)

  const unavailable = new Error('HTTP 503')
  unavailable.status = 503
  assert.match(sendFailureMessage(unavailable), /can’t save messages right now/)

  assert.match(sendFailureMessage(new Error('HTTP 400')), /couldn’t send the message/)
  assert.match(sendFailureMessage({ status: 429 }), /too many requests/)
  assert.match(sendFailureMessage({ status: 401 }), /sign in again/)
})

test('an unrelated programming TypeError is not mislabeled as offline', () => {
  assert.doesNotMatch(
    sendFailureMessage(new TypeError('cannot read property')),
    /check your connection/,
  )
})

test('known offline state wins over the transport error shape', () => {
  assert.match(
    sendFailureMessage(new Error('anything'), { online: false }),
    /You’re offline/,
  )
  assert.match(
    sendFailureMessage({ outboxRetained: true }, { online: false }),
    /queued and will send when you reconnect/,
  )
  assert.match(
    sendFailureMessage(new Error('anything'), { online: false }),
    /back in the composer/,
  )
})

test('automatic replay is promised only when the durable write succeeded', () => {
  const transport = new ChatTransportError(new TypeError('Failed to fetch'))
  transport.outboxRetained = true
  assert.match(sendFailureMessage(transport), /queued and will retry automatically/)

  const unavailable = new Error('HTTP 503')
  unavailable.status = 503
  unavailable.outboxRetained = true
  assert.match(sendFailureMessage(unavailable), /queued and will retry automatically/)

  assert.match(
    sendFailureMessage({ status: 401, outboxRetained: true }),
    /queued for this owner and will resume afterward/,
  )
})

test('HTTP failures retain a safe server detail for diagnostics', async () => {
  const error = await chatHttpError({
    status: 503,
    async json() {
      return { detail: { code: 'writer_unavailable', message: 'writer unavailable' } }
    },
  })
  assert.equal(error instanceof ChatHttpError, true)
  assert.equal(error.status, 503)
  assert.equal(error.code, 'writer_unavailable')
  assert.equal(error.detail, 'writer unavailable')
})

test('a pending-question conflict explains the recovery instead of reporting a generic failure', () => {
  const error = new ChatHttpError(409, { code: 'pending_question_open' })

  assert.equal(isPendingQuestionSendFailure(error), true)
  assert.match(sendFailureMessage(error), /Answer the pending question above/)
  assert.match(sendFailureMessage(error), /safe in the composer/)
  assert.equal(isPendingQuestionSendFailure(new ChatHttpError(409, {
    code: 'another_conflict',
  })), false)
})

test('a missing model is a distinct recoverable send conflict', () => {
  const error = new ChatHttpError(409, { code: 'model_selection_required' })

  assert.equal(isModelSelectionRequiredFailure(error), true)
  assert.match(sendFailureMessage(error), /Choose a model before sending/)
  assert.match(sendFailureMessage(error), /safe in the composer/)
  assert.equal(isModelSelectionRequiredFailure(new ChatHttpError(409, {
    code: 'another_conflict',
  })), false)
})
