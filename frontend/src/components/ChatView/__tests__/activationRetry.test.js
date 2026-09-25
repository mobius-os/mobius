import { test } from 'node:test'
import assert from 'node:assert/strict'
import { activationRetryDelay } from '../chatRuntimeState.js'

const READ_TIMEOUT_MS = 15000
const timeout = Object.assign(new Error('Request timed out'), { name: 'TimeoutError' })

test('activation retries only transient failures, a bounded number of times', () => {
  assert.equal(activationRetryDelay(new TypeError('Failed to fetch'), 0, READ_TIMEOUT_MS), 1000)
  assert.equal(activationRetryDelay(new TypeError('NetworkError when attempting to fetch resource.'), 0, READ_TIMEOUT_MS), 1000)
  assert.equal(activationRetryDelay(new TypeError('Cannot read properties of undefined'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 1, READ_TIMEOUT_MS), 3000)
  assert.equal(activationRetryDelay(new Error('CHAT_LOAD_FAILED_429'), 2, READ_TIMEOUT_MS), 8000)
  assert.equal(activationRetryDelay(new Error('CHAT_NOT_FOUND'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_403'), 0, READ_TIMEOUT_MS), null)
  assert.equal(activationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 5, READ_TIMEOUT_MS), null)
})

test('activation retries span a server restart', () => {
  const error = new Error('CHAT_LOAD_FAILED_503')
  let total = 0
  for (let attempt = 0; activationRetryDelay(error, attempt, READ_TIMEOUT_MS) != null; attempt += 1) {
    total += activationRetryDelay(error, attempt, READ_TIMEOUT_MS)
  }
  assert.ok(total >= 45000)
})

test('a timed-out read is never retried while it may still be running server-side', () => {
  for (let attempt = 0; activationRetryDelay(timeout, attempt, READ_TIMEOUT_MS) != null; attempt += 1) {
    assert.ok(activationRetryDelay(timeout, attempt, READ_TIMEOUT_MS) >= READ_TIMEOUT_MS)
  }
})
