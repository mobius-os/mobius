import test from 'node:test'
import assert from 'node:assert/strict'

import { sendFailureMessage } from '../sendFailure.js'
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
})

test('HTTP failures retain a safe server detail for diagnostics', async () => {
  const error = await chatHttpError({
    status: 503,
    async json() { return { detail: { message: 'writer unavailable' } } },
  })
  assert.equal(error instanceof ChatHttpError, true)
  assert.equal(error.status, 503)
  assert.equal(error.detail, 'writer unavailable')
})
