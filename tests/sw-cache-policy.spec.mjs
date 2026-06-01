/**
 * Service-worker cache policy — locks in the fix for the cache-poisoning
 * class where a missing /vendor file fell through to the SPA's
 * `200 text/html`, got cached cache-first, and was then served forever
 * in place of the real module ("failed to load dynamic module").
 *
 * These are pure functions (frontend/src/sw-cache-policy.js), so no
 * browser/SW context is needed.
 *
 * Run: npx playwright test tests/sw-cache-policy.spec.mjs
 */
import { test, expect } from '@playwright/test'
import {
  VENDOR_CACHE,
  ESM_CACHE,
  isCacheableAssetResponse,
  isStaleRuntimeCache,
  isKnownOnline,
  shouldServeCacheFirst,
  shouldFallBackToCacheOnError,
  VERDICT_MAX_AGE_MS,
} from '../frontend/src/sw-cache-policy.js'

const resp = (status, ct) => ({ status, headers: { get: () => ct } })

test.describe('sw cache policy — isCacheableAssetResponse', () => {
  test('caches genuine module/asset content types', () => {
    expect(isCacheableAssetResponse(resp(200, 'application/javascript; charset=utf-8'))).toBe(true)
    expect(isCacheableAssetResponse(resp(200, 'text/javascript'))).toBe(true)
    expect(isCacheableAssetResponse(resp(200, 'text/css'))).toBe(true)
    expect(isCacheableAssetResponse(resp(200, 'application/wasm'))).toBe(true)
  })

  test('refuses SPA HTML, plain-text errors, and non-200 (the poison)', () => {
    expect(isCacheableAssetResponse(resp(200, 'text/html; charset=utf-8'))).toBe(false)
    expect(isCacheableAssetResponse(resp(200, 'text/plain'))).toBe(false)
    expect(isCacheableAssetResponse(resp(404, 'application/javascript'))).toBe(false)
    expect(isCacheableAssetResponse(null)).toBe(false)
  })
})

test.describe('sw cache policy — isStaleRuntimeCache', () => {
  test('evicts the poisoned un-suffixed and legacy caches', () => {
    expect(isStaleRuntimeCache('mobius-vendor')).toBe(true)
    expect(isStaleRuntimeCache('mobius-esm')).toBe(true)
    expect(isStaleRuntimeCache('mobius-vendor-v1')).toBe(true)
    expect(isStaleRuntimeCache('mobius-assets-v3')).toBe(true)
    expect(isStaleRuntimeCache('mobius-proxy-v9')).toBe(true)
  })

  test('keeps the current v2 runtime caches (no per-deploy refetch)', () => {
    expect(isStaleRuntimeCache(VENDOR_CACHE)).toBe(false)
    expect(isStaleRuntimeCache(ESM_CACHE)).toBe(false)
  })

  test('leaves non-mobius caches (e.g. workbox precache) untouched', () => {
    expect(isStaleRuntimeCache('workbox-precache-v2-https://x/')).toBe(false)
    expect(isStaleRuntimeCache('mobius-proxy')).toBe(false)
  })
})

test.describe('sw cache policy — isKnownOnline (offline-capable frame/module gate)', () => {
  const NOW = 1_000_000

  test('fresh positive verdict → known online (network-first keeps app code fresh)', () => {
    expect(isKnownOnline(true, NOW - 1000, NOW)).toBe(true)
  })

  test('fresh negative verdict → NOT known online (cache-first instant offline)', () => {
    expect(isKnownOnline(false, NOW - 1000, NOW)).toBe(false)
  })

  test('unknown verdict (cold SW restart, no postMessage yet) → NOT known online → cache-first, no race', () => {
    // The cold-start win: undefined !== true, so a cached app is served instantly
    // on the very first request without waiting for the verdict to arrive.
    expect(isKnownOnline(undefined, 0, NOW)).toBe(false)
  })

  test('stale positive verdict → NOT known online (do not trust an old "online" across a gap)', () => {
    expect(isKnownOnline(true, NOW - (VERDICT_MAX_AGE_MS + 1), NOW)).toBe(false)
    // exactly at the boundary is stale (strict <)
    expect(isKnownOnline(true, NOW - VERDICT_MAX_AGE_MS, NOW)).toBe(false)
    // just inside the window is fresh
    expect(isKnownOnline(true, NOW - (VERDICT_MAX_AGE_MS - 1), NOW)).toBe(true)
  })
})

test.describe('sw cache policy — shouldServeCacheFirst (frame/module serve strategy)', () => {
  test('cached + NOT known-online → cache-first (instant offline / cold-restart)', () => {
    expect(shouldServeCacheFirst(true, false)).toBe(true)
  })
  test('cached + known-online → network-first (agent edit fresh on current open)', () => {
    expect(shouldServeCacheFirst(true, true)).toBe(false)
  })
  test('no cache + NOT known-online → network (cold path, nothing to serve)', () => {
    expect(shouldServeCacheFirst(false, false)).toBe(false)
  })
  test('no cache + known-online → network', () => {
    expect(shouldServeCacheFirst(false, true)).toBe(false)
  })
})

test.describe('sw cache policy — shouldFallBackToCacheOnError (5xx resilience)', () => {
  test('5xx + cached → serve cache (transient server error must not blank a cached app)', () => {
    expect(shouldFallBackToCacheOnError(500, true)).toBe(true)
    expect(shouldFallBackToCacheOnError(502, true)).toBe(true)
    expect(shouldFallBackToCacheOnError(503, true)).toBe(true)
  })
  test('5xx + NO cache → return the error (nothing to fall back to)', () => {
    expect(shouldFallBackToCacheOnError(500, false)).toBe(false)
  })
  test('4xx → authoritative, never masked by cache (404 gone / 401 auth)', () => {
    expect(shouldFallBackToCacheOnError(404, true)).toBe(false)
    expect(shouldFallBackToCacheOnError(401, true)).toBe(false)
    expect(shouldFallBackToCacheOnError(403, true)).toBe(false)
  })
  test('2xx/3xx → real response, never replaced', () => {
    expect(shouldFallBackToCacheOnError(200, true)).toBe(false)
    expect(shouldFallBackToCacheOnError(304, true)).toBe(false)
  })
})
