import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  MAX_PENDING_TRACE_RETRIES,
  pendingTraceRetryDelay,
} from '../useThinkingTrace.js'

const source = readFileSync(new URL('../useThinkingTrace.js', import.meta.url), 'utf8')

test('thinking trace retries are bounded and honor Retry-After', () => {
  assert.equal(MAX_PENDING_TRACE_RETRIES, 5)
  assert.equal(pendingTraceRetryDelay('1', 1), 1000)
  assert.equal(pendingTraceRetryDelay('0', 1), 250)
  assert.equal(pendingTraceRetryDelay('30', 1), 5000)
  assert.match(source, /pendingRetries >= MAX_PENDING_TRACE_RETRIES/)
  assert.match(source, /res\.headers\.get\('Retry-After'\)/)
})

test('missing Retry-After falls back to capped exponential backoff', () => {
  assert.equal(pendingTraceRetryDelay(null, 1), 1000)
  assert.equal(pendingTraceRetryDelay('', 2), 2000)
  assert.equal(pendingTraceRetryDelay('invalid', 3), 4000)
  assert.equal(pendingTraceRetryDelay(undefined, 8), 5000)
})
