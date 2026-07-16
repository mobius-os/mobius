/**
 * Service-worker cache policy — locks in the fix for the cache-poisoning
 * class where a missing /vendor file fell through to the SPA's
 * `200 text/html`, got cached cache-first, and was then served forever
 * in place of the real module ("failed to load dynamic module").
 *
 * These are pure functions (frontend/src/sw-cache-policy.js), so no
 * browser/SW context is needed.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/sw-cache-policy.spec.mjs
 */
import { test, expect } from '@playwright/test'
import {
  VENDOR_CACHE,
  ESM_CACHE,
  APP_ASSETS_CACHE,
  isCacheableAssetResponse,
  isRangeRequest,
  isPackagedAppAsset,
  packagedAppAssetCacheKey,
  isCacheableOpaqueEmbedDocument,
  isStaleRuntimeCache,
  shouldServeCacheFirst,
  shouldFallBackToCacheOnError,
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

test.describe('sw cache policy — app-assets poisoned-cache eviction', () => {
  test('evicts the v1 app-assets cache (held 1-byte ranged-slice bodies)', () => {
    expect(isStaleRuntimeCache('mobius-app-assets-v1')).toBe(true)
  })

  test('keeps the current app-assets cache', () => {
    expect(isStaleRuntimeCache(APP_ASSETS_CACHE)).toBe(false)
  })
})

test.describe('sw cache policy — opaque packaged documents', () => {
  test('shares subresource identity but not the sandboxed document identity', () => {
    const entry = 'https://mobius.test/app-embeds/by-id/60/index.html'
    const script = 'https://mobius.test/app-embeds/by-id/60/static/main.deadbeef.js'
    expect(isPackagedAppAsset(new URL(entry).pathname)).toBe(true)
    expect(packagedAppAssetCacheKey(entry, true)).toBe(entry)
    expect(packagedAppAssetCacheKey(script, false)).toBe(
      'https://mobius.test/app-assets/by-id/60/static/main.deadbeef.js'
    )
  })

  test('refuses an entry document if CSP sandbox is missing or same-origin', () => {
    const response = csp => ({
      status: 200,
      headers: { get: name => name === 'content-type' ? 'text/html' : csp },
    })
    expect(isCacheableOpaqueEmbedDocument(response('sandbox allow-scripts'))).toBe(true)
    expect(isCacheableOpaqueEmbedDocument(response("default-src 'self'"))).toBe(false)
    expect(isCacheableOpaqueEmbedDocument(response(
      'sandbox allow-scripts allow-same-origin',
    ))).toBe(false)
  })
})

test.describe('sw cache policy — isRangeRequest (ranged fetches bypass caches)', () => {
  const req = (headers) => ({
    headers: { has: (name) => Object.hasOwn(headers, name) },
  })

  test('a Range-bearing request is detected (the CubeRun probe shape)', () => {
    expect(isRangeRequest(req({ range: 'bytes=0-0' }))).toBe(true)
  })

  test('plain requests and degenerate inputs are not ranged', () => {
    expect(isRangeRequest(req({}))).toBe(false)
    expect(isRangeRequest(null)).toBe(false)
    expect(isRangeRequest({})).toBe(false)
  })
})

test.describe('sw cache policy — shouldServeCacheFirst (versioned app-code strategy)', () => {
  test('cached + NOT known-online → cache-first (instant offline / cold-restart)', () => {
    expect(shouldServeCacheFirst(true, false)).toBe(true)
  })
  test('cached + known-online → cache-first (versioned URL keeps app edits fresh)', () => {
    expect(shouldServeCacheFirst(true, true)).toBe(true)
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
