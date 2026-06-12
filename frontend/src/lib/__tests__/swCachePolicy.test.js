import test from 'node:test'
import assert from 'node:assert/strict'

import {
  APP_ASSETS_CACHE,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  VENDOR_CACHE,
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

const res = (status, type) => ({
  status,
  headers: { get: () => type },
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
