import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'node:test'

import compactReporter from '../../../scripts/compact-test-reporter.mjs'

// The reporter writes its full log to the same per-checkout path as the run
// executing this test. Point it at a scratch file so a passing run's log is
// not overwritten by these synthetic failures.
async function report(events) {
  const directory = mkdtempSync(path.join(os.tmpdir(), 'compact-reporter-'))
  const previous = process.env.MOBIUS_TEST_LOG
  process.env.MOBIUS_TEST_LOG = path.join(directory, 'full.log')
  try {
    let text = ''
    for await (const chunk of compactReporter((async function* () { yield* events })())) text += chunk
    return text
  } finally {
    if (previous === undefined) delete process.env.MOBIUS_TEST_LOG
    else process.env.MOBIUS_TEST_LOG = previous
    rmSync(directory, { recursive: true, force: true })
  }
}

function failingRun(name, error, nesting = 0) {
  return [
    { type: 'test:fail', data: { name, nesting, file: '/tmp/x.test.js', line: 3, details: { error } } },
    { type: 'test:summary', data: { success: false, counts: { tests: 1, passed: 0, failed: 1 }, duration_ms: 5 } },
  ]
}

test('compact reporter describes a failure whose values String() cannot convert', async () => {
  const message = Object.create(null)
  message.reason = 'kept for the reader'
  const cause = Object.create(null)
  cause.message = message
  cause.name = Object.create(null)
  const error = new Error('test failed', { cause })
  error.failureType = 'testCodeFailure'

  const text = await report(failingRun('throws a bare object', error))

  assert.match(text, /✖ throws a bare object/)
  assert.match(text, /kept for the reader/)
  assert.match(text, /FAILED - 0\/1 passed, 1 failed/)
})

test('compact reporter describes an Error whose stack cannot stringify its message or name', async () => {
  // V8 builds error.stack on first read from the current message and name,
  // which describing a nested test's failure reads.
  const nullMessage = new Error('placeholder')
  nullMessage.message = Object.create(null)
  const symbolName = new Error('placeholder')
  symbolName.name = Symbol('odd name')

  for (const [name, error] of [['null-prototype message', nullMessage], ['Symbol name', symbolName]]) {
    const text = await report(failingRun(name, error, 1))
    assert.match(text, new RegExp(`✖ ${name}`))
    assert.match(text, /FAILED - 0\/1 passed, 1 failed/)
  }
})
