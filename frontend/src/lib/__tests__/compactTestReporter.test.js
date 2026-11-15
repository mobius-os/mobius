import assert from 'node:assert/strict'
import { test } from 'node:test'

import compactReporter from '../../../scripts/compact-test-reporter.mjs'

async function report(events) {
  let text = ''
  for await (const chunk of compactReporter((async function* () { yield* events })())) text += chunk
  return text
}

test('compact reporter describes a failure whose values String() cannot convert', async () => {
  // A cross-realm or null-prototype value has no toString to fall back on.
  const message = Object.create(null)
  message.reason = 'kept for the reader'
  const cause = Object.create(null)
  cause.message = message
  cause.name = Object.create(null)
  const error = new Error('test failed', { cause })
  error.failureType = 'testCodeFailure'

  const text = await report([
    { type: 'test:fail', data: { name: 'throws a bare object', nesting: 0, file: '/tmp/x.test.js', line: 3, details: { error } } },
    { type: 'test:summary', data: { success: false, counts: { tests: 1, passed: 0, failed: 1 }, duration_ms: 5 } },
  ])

  assert.match(text, /✖ throws a bare object/)
  assert.match(text, /kept for the reader/)
  assert.match(text, /FAILED - 0\/1 passed, 1 failed/)
})
