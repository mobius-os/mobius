import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import {
  MAX_PENDING_TRACE_RETRIES,
  pendingTraceRetryDelay,
} from '../useThinkingTrace.js'

const source = readFileSync(new URL('../useThinkingTrace.js', import.meta.url), 'utf8')
const sidecarSource = readFileSync(new URL('../lazySidecar.js', import.meta.url), 'utf8')

test('thinking trace retries are bounded and honor Retry-After', () => {
  assert.equal(MAX_PENDING_TRACE_RETRIES, 5)
  assert.equal(pendingTraceRetryDelay('1', 1), 1000)
  assert.equal(pendingTraceRetryDelay('0', 1), 250)
  assert.equal(pendingTraceRetryDelay('30', 1), 5000)
  assert.match(source, /fetchLazyText\(url, \{ signal: controller\.signal \}\)/)
  assert.match(sidecarSource, /pendingRetries >= MAX_PENDING_SIDECAR_RETRIES/)
  assert.match(sidecarSource, /response\.headers\.get\('Retry-After'\)/)
  assert.match(sidecarSource, /await abortableDelay/)
})

test('missing Retry-After falls back to capped exponential backoff', () => {
  assert.equal(pendingTraceRetryDelay(null, 1), 1000)
  assert.equal(pendingTraceRetryDelay('', 2), 2000)
  assert.equal(pendingTraceRetryDelay('invalid', 3), 4000)
  assert.equal(pendingTraceRetryDelay(undefined, 8), 5000)
})

test('thinking expansion is bounded until full content is explicitly requested', () => {
  assert.match(source, /\+ \(fullRequested \? '' : '&preview=1'\)/)
  assert.match(source, /loadFull: \(\) => \{\s*setFullRequested\(true\)/)
  assert.match(source, /setFullRequested\(false\)[\s\S]*setRefreshNonce/,
    'a growing live trace returns to previews instead of repeatedly loading full text')
  assert.match(source, /if \(!open && deferred\) \{[\s\S]*setLoadedContent\(''\)/,
    'closing the thought releases its loaded payload')
  assert.match(source, /traceComplete \|\| !!thought\.thinking_complete/,
    'final promotion unlocks explicit full loading without a background completion fetch')
})
