import { test } from 'node:test'
import assert from 'node:assert/strict'
import { activationRetryDelay, chatEntryFrame } from '../chatRuntimeState.js'

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

const frame = (overrides) => chatEntryFrame({
  messageCount: 0,
  loading: false,
  loadError: false,
  activationRetrying: false,
  turnActive: false,
  activationPhase: 'pending',
  activationSettled: false,
  transcriptPaintable: false,
  ...overrides,
})

test('an empty chat retrying its load presents a stable error frame, not the empty state', () => {
  // Shell holds the launch cover or the previous chat until display-ready, so
  // quiet retries must not keep it waiting for up to minutes.
  for (const activationPhase of ['error', 'pending', 'cold']) {
    for (const loading of [true, false]) {
      assert.deepEqual(
        frame({ activationRetrying: true, activationPhase, loading }),
        { showEmpty: false, showLoadError: true, displayReady: true },
        `${activationPhase}, loading=${loading}`,
      )
    }
  }
  assert.equal(frame({ activationRetrying: true, messageCount: 3 }).showLoadError, false,
    'rows a successful retry is preparing replace the retry frame')
})

test('a load error is stable at once and wins over a cached running marker', () => {
  assert.deepEqual(
    frame({ loadError: true, turnActive: true, activationPhase: 'error' }),
    { showEmpty: false, showLoadError: true, displayReady: true },
  )
  assert.equal(frame({ loadError: true, loading: true }).showLoadError, false,
    'a manual retry hides the error while it loads')
})

test('a transcript or empty frame waits for runtime truth; only cold or cached-fallback activation is early', () => {
  const transcript = { messageCount: 5, transcriptPaintable: true }
  assert.equal(frame({ ...transcript, activationPhase: 'pending' }).displayReady, false)
  assert.equal(frame({ ...transcript, activationPhase: 'cold' }).displayReady, true)
  assert.equal(frame({ ...transcript, activationPhase: 'error' }).displayReady, true)
  assert.equal(frame({ ...transcript, activationPhase: 'error', loading: true }).displayReady, false)
  assert.equal(frame({ ...transcript, activationSettled: true, loading: true }).displayReady, false)
  assert.equal(frame({ ...transcript, activationSettled: true }).displayReady, true)
  assert.deepEqual(frame({}), { showEmpty: true, showLoadError: false, displayReady: false })
  assert.deepEqual(
    frame({ activationSettled: true }),
    { showEmpty: true, showLoadError: false, displayReady: true },
  )
})
