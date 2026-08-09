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

test('an open thought shows its full text and never blanks', () => {
  // No bounded preview and no exact-revision pin: pull the whole trace the
  // server has now, so there is never a "bounded preview" and a live thought
  // cannot hang on "Loading…" waiting for a revision the server hasn't written.
  assert.doesNotMatch(source, /preview=1/, 'no bounded-preview fetch')
  assert.doesNotMatch(source, /[?&]revision=/, 'no exact-revision pin that could 202-hang')
  assert.doesNotMatch(source, /loadFull/, 'full is the default; no explicit full-load step')
  assert.match(source, /thinking-trace\/\$\{encodeURIComponent\(thought\.thinking_id\)\}/)
  // Inline bridge + honest load state keep text on screen across the
  // inline->deferred crossover and through background re-fetches / failures.
  assert.match(source, /bridgeRef\.current = thought\.content/,
    'captures the inline prefix before the server defers the thought')
  assert.match(source, /loadedContent \|\| bridgeRef\.current/,
    'shows the bridge until the first full fetch lands')
  assert.match(source, /loadState: content \? 'ready' : loadState/,
    'a re-fetch or transient failure never blanks text already on screen')
  assert.match(source, /if \(!open && deferred\) \{[\s\S]*setLoadedContent\(''\)/,
    'closing drops the fetched copy — no persistent local copy')
})
