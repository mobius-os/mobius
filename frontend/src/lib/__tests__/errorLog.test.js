/**
 * Unit tests for errorLog.js's remote sink (#1B shell-error -> activity).
 *
 * recordClientError must, IN ADDITION to its console + sessionStorage ring,
 * POST uncaught shell errors to /api/client-error so they land in the activity
 * log as `app_error` events. Contract locked in here:
 *   - POSTs once with Bearer token + message/where/url body, keepalive.
 *   - Debounces a repeated identical message within the 60s window.
 *   - No-op before login (no token) — never POSTs unauthenticated.
 *   - Never throws, even if fetch rejects.
 *
 * Run with:
 *   cd frontend && node --loader=./src/lib/__tests__/vite-env-loader.mjs \
 *     --test src/lib/__tests__/errorLog.test.js
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

let fetchCalls
let errorLog

function stubEnv({ token = 'owner-jwt' } = {}) {
  fetchCalls = []
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (k === 'token' ? token : null),
    setItem: () => {},
    removeItem: () => {},
  }
  globalThis.sessionStorage = {
    getItem: (k) => store.get(k) ?? null,
    setItem: (k, v) => store.set(k, v),
    removeItem: (k) => store.delete(k),
  }
  globalThis.location = { href: 'https://mobius.example/app' }
  // Swallow the console.error the logger always emits so test output stays clean.
  globalThis.console = { ...console, error: () => {} }
  globalThis.fetch = (url, opts) => {
    fetchCalls.push({ url, opts })
    return Promise.resolve({ ok: true })
  }
}

beforeEach(async () => {
  stubEnv()
  // Fresh module each test so the in-module debounce Map doesn't leak across cases.
  const url = new URL('../errorLog.js', import.meta.url).href + `?t=${Math.random()}`
  errorLog = await import(url)
})

test('recordClientError POSTs the shell error to /api/client-error', () => {
  errorLog.recordClientError({ where: 'window.onerror', message: 'Boom', stack: 'at x' })
  assert.equal(fetchCalls.length, 1, 'exactly one POST')
  const { url, opts } = fetchCalls[0]
  assert.ok(url.endsWith('/api/client-error'), `posts to /api/client-error, got ${url}`)
  assert.equal(opts.method, 'POST')
  assert.equal(opts.keepalive, true, 'keepalive so it survives a crash/reload')
  assert.equal(opts.headers.Authorization, 'Bearer owner-jwt')
  const body = JSON.parse(opts.body)
  assert.equal(body.message, 'Boom')
  assert.equal(body.where, 'window.onerror')
  assert.equal(body.url, 'https://mobius.example/app')
})

test('repeated identical message within the window is debounced', () => {
  errorLog.recordClientError({ where: 'w', message: 'same error' })
  errorLog.recordClientError({ where: 'w', message: 'same error' })
  assert.equal(fetchCalls.length, 1, 'second identical error within 60s must not re-POST')
})

test('a different message still POSTs', () => {
  errorLog.recordClientError({ where: 'w', message: 'first' })
  errorLog.recordClientError({ where: 'w', message: 'second' })
  assert.equal(fetchCalls.length, 2)
})

test('no token (pre-login) → no POST', async () => {
  stubEnv({ token: null })
  const url = new URL('../errorLog.js', import.meta.url).href + `?t=${Math.random()}`
  const fresh = await import(url)
  fresh.recordClientError({ where: 'w', message: 'crash before login' })
  assert.equal(fetchCalls.length, 0, 'must not POST without a token')
})

test('recordClientError never throws even if fetch rejects', () => {
  globalThis.fetch = () => Promise.reject(new Error('network down'))
  assert.doesNotThrow(() =>
    errorLog.recordClientError({ where: 'w', message: 'still safe' }),
  )
})

test('still writes the sessionStorage ring (existing behavior preserved)', () => {
  errorLog.recordClientError({ where: 'w', message: 'ring me' })
  const ring = errorLog.getRecentErrors()
  assert.ok(ring.some((r) => r.message === 'ring me'), 'ring buffer still populated')
})
