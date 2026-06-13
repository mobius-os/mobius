import test from 'node:test'
import assert from 'node:assert/strict'

import {
  APP_ASSETS_CACHE,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  VENDOR_CACHE,
  appCodeStoreAction,
  isAppCodeRoute,
  isCacheableAppAssetResponse,
  isImmutableAppAsset,
  isStaleRuntimeCache,
} from '../../sw-cache-policy.js'

test('runtime cache cleanup keeps current cache names', () => {
  for (const name of [
    VENDOR_CACHE,
    ESM_CACHE,
    OFFLINE_APPS_CACHE,
    STANDALONE_APPS_CACHE,
    APP_ASSETS_CACHE,
  ]) {
    assert.equal(isStaleRuntimeCache(name), false, `${name} should be kept`)
  }
})

test('runtime cache cleanup evicts old offline app caches', () => {
  assert.equal(isStaleRuntimeCache('mobius-offline-apps'), true)
  assert.equal(isStaleRuntimeCache('mobius-offline-apps-v1'), true)
  assert.equal(isStaleRuntimeCache('mobius-standalone'), true)
  assert.equal(isStaleRuntimeCache('mobius-standalone-v1'), true)
})

test('hashed packaged-app asset names are immutable', () => {
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/static/js/main.8f3a2b1c.js'),
    true,
  )
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/chunk-a1b2c3d4e5f6.js'),
    true,
  )
})

test('un-hashed or non-app-asset paths are not immutable', () => {
  // index.html and plain names can be replaced in place on re-install.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/index.html'), false)
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/main.js'), false)
  // A short hex-looking word is not a content hash.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/cafe.js'), false)
  // A hash in a DIRECTORY segment doesn't make the file immutable.
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/a1b2c3d4e5f6aa/main.js'),
    false,
  )
  // Other routes never match, hash or not.
  assert.equal(isImmutableAppAsset('/vendor/main.8f3a2b1c.js'), false)
})

test('all-digit (date-stamp) segments are not mistaken for hashes', () => {
  // A date-stamped name is replaced in place on re-upload, so marking it
  // immutable would pin a year-stale copy. The lookahead requiring an
  // alphabetic hex digit keeps these revalidating.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/IMG-20260612.png'), false)
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/report.20260101.html'),
    false,
  )
  // A delimited digest that mixes in a-f still counts as immutable; the
  // all-digit gate only rejects segments with no alphabetic hex char.
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/bundle.cafe1234.js'), true,
  )
})

const res = (status, type) => ({
  status,
  headers: { get: () => type },
})

test('app-code route matches frame and module for any app id', () => {
  assert.equal(isAppCodeRoute('/api/apps/1/frame'), true)
  assert.equal(isAppCodeRoute('/api/apps/1/module'), true)
  assert.equal(isAppCodeRoute('/api/apps/4203/module'), true)
})

test('app-code route rejects everything else', () => {
  // Other per-app endpoints must keep their network-only behavior.
  assert.equal(isAppCodeRoute('/api/apps/1/validate'), false)
  assert.equal(isAppCodeRoute('/api/apps/1'), false)
  // Non-numeric ids and nested paths never match.
  assert.equal(isAppCodeRoute('/api/apps/abc/frame'), false)
  assert.equal(isAppCodeRoute('/api/apps/1/frame/extra'), false)
  // Standalone navigations go through the gated route, not this one.
  assert.equal(isAppCodeRoute('/apps/notes/'), false)
})

test('ungated frame/module reads store every 200', () => {
  // Loading speed must not depend on the offline_capable flag: a 200
  // without the X-Mobius-Offline header is stored all the same.
  assert.equal(appCodeStoreAction(200, '1', false), 'store')
  assert.equal(appCodeStoreAction(200, null, false), 'store')
})

test('gated standalone navigations keep the offline_capable contract', () => {
  assert.equal(appCodeStoreAction(200, '1', true), 'store')
  // A 200 WITHOUT the header purges: the app was toggled
  // offline_capable off and the stale entry must self-heal away.
  assert.equal(appCodeStoreAction(200, null, true), 'purge')
})

test('non-200 responses are never stored or purged', () => {
  // 304s have no body; 4xx/5xx must not evict a known-good entry —
  // shouldFallBackToCacheOnError owns what the page sees instead.
  for (const gated of [false, true]) {
    assert.equal(appCodeStoreAction(304, '1', gated), 'ignore')
    assert.equal(appCodeStoreAction(404, '1', gated), 'ignore')
    assert.equal(appCodeStoreAction(500, '1', gated), 'ignore')
  }
})

test('immutable app-asset cache refuses non-200 and HTML bodies', () => {
  // Models/textures/fonts/audio are all fine — only a document body at a
  // hashed URL (fallback/error page) is the poisoning class.
  assert.equal(
    isCacheableAppAssetResponse(res(200, 'model/gltf-binary')), true,
  )
  assert.equal(
    isCacheableAppAssetResponse(res(200, 'application/octet-stream')), true,
  )
  assert.equal(isCacheableAppAssetResponse(res(200, 'text/html')), false)
  assert.equal(
    isCacheableAppAssetResponse(res(304, 'application/octet-stream')), false,
  )
  assert.equal(isCacheableAppAssetResponse(null), false)
})
