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
