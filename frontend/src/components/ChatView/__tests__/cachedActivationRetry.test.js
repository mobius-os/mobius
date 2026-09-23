import { test } from 'node:test'
import assert from 'node:assert/strict'
import { cachedActivationRetryDelay } from '../chatRuntimeState.js'

test('cached activation retries only transient failures and stops after three attempts', () => {
  assert.equal(cachedActivationRetryDelay(new TypeError('Failed to fetch'), 0), 750)
  assert.equal(cachedActivationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 1), 2000)
  assert.equal(cachedActivationRetryDelay(new Error('CHAT_LOAD_FAILED_429'), 2), 5000)
  assert.equal(cachedActivationRetryDelay(new Error('CHAT_NOT_FOUND'), 0), null)
  assert.equal(cachedActivationRetryDelay(new Error('CHAT_RUNTIME_FAILED_403'), 0), null)
  assert.equal(cachedActivationRetryDelay(new Error('CHAT_RUNTIME_FAILED_503'), 3), null)
})
