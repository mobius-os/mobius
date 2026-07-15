import test from 'node:test'
import assert from 'node:assert/strict'

import { sendFailureMessage } from '../sendFailure.js'

test('send failures distinguish connection, timeout, service, and generic errors', () => {
  assert.match(sendFailureMessage(new TypeError('Failed to fetch')), /check your connection/)

  const timeout = new Error('aborted')
  timeout.name = 'AbortError'
  assert.match(sendFailureMessage(timeout), /too long/)

  const unavailable = new Error('HTTP 503')
  unavailable.status = 503
  assert.match(sendFailureMessage(unavailable), /can’t save messages right now/)

  assert.match(sendFailureMessage(new Error('HTTP 400')), /couldn’t send the message/)
})

test('known offline state wins over the transport error shape', () => {
  assert.match(
    sendFailureMessage(new Error('anything'), { online: false }),
    /check your connection/,
  )
})
