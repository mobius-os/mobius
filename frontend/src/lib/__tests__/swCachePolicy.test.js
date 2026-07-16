import test from 'node:test'
import assert from 'node:assert/strict'

import {
  APP_ASSETS_CACHE,
  APP_ASSETS_MAX_ENTRIES,
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  VENDOR_CACHE,
  appCodeStoreAction,
  entriesToTrim,
  isAppCodeRoute,
  isCacheableAppAssetResponse,
  hasOpaqueEmbedSandbox,
  isCacheableOpaqueEmbedDocument,
  isImmutableAppAsset,
  isPackagedAppAsset,
  packagedAppAssetCacheKey,
  isStaleRuntimeCache,
  supersededVersionKeys,
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
  // v2 superseded by the -v3 bump (frame-rev cache-key fix). Must be evicted on
  // activate so installed PWAs drop frames cached under the pre-fix un-revved key.
  assert.equal(isStaleRuntimeCache('mobius-offline-apps-v2'), true)
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
  // An all-alpha hex digest (deadbeef) is still a content hash.
  assert.equal(
    isImmutableAppAsset('/app-assets/cuberun/chunk.deadbeef.js'),
    true,
  )
  // Exactly 8 chars with a single alpha char at the boundary still counts.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/x.1234567a.js'), true)
  assert.equal(
    isImmutableAppAsset('/app-embeds/by-id/60/static/js/main.8f3a2b1c.js'),
    true,
  )
})

test('only controlled packaged subresources reuse ordinary by-id cache keys', () => {
  const embed = 'https://mobius.test/app-embeds/by-id/60/static/js/main.deadbeef.js'
  const ordinary = 'https://mobius.test/app-assets/by-id/60/static/js/main.deadbeef.js'
  const entry = 'https://mobius.test/app-embeds/by-id/60/index.html'
  assert.equal(isPackagedAppAsset(new URL(embed).pathname), true)
  assert.equal(packagedAppAssetCacheKey(embed, { isSubresource: true }), ordinary)
  // Documents and fetch()/XHR keep the response-sandboxed alias identity.
  assert.equal(packagedAppAssetCacheKey(entry, { isDocument: true }), entry)
  assert.equal(packagedAppAssetCacheKey(entry), entry)
  assert.equal(packagedAppAssetCacheKey(ordinary, { isSubresource: true }), ordinary)
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

test('all-digit version segments are NOT immutable (alpha-hex required)', () => {
  // The all-digit false-positive: a version number or timestamp in the
  // filename is reused across re-installs, so it must revalidate — caching
  // it forever pins a stale build. >=1 alpha hex char (a-f) is required.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/main.12345678.js'), false)
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/app.20260612.js'), false)
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/logo.00000000.png'), false)
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/v1.99999999.css'), false)
  // A 7-char hex run is too short even WITH an alpha char.
  assert.equal(isImmutableAppAsset('/app-assets/cuberun/x.123456a.js'), false)
})

const res = (status, type) => ({
  status,
  headers: { get: () => type },
})

const documentRes = (status, type, csp) => ({
  status,
  headers: { get: name => name === 'content-type' ? type : csp },
})

test('opaque embed documents cache only with the response sandbox intact', () => {
  assert.equal(isCacheableOpaqueEmbedDocument(documentRes(
    200, 'text/html; charset=utf-8', "sandbox allow-scripts; default-src 'self'",
  )), true)
  assert.equal(isCacheableOpaqueEmbedDocument(documentRes(
    200, 'text/html', "sandbox allow-scripts allow-same-origin",
  )), false)
  assert.equal(isCacheableOpaqueEmbedDocument(documentRes(
    200, 'text/html', "default-src 'self'",
  )), false)
  assert.equal(isCacheableOpaqueEmbedDocument(documentRes(
    404, 'text/html', 'sandbox allow-scripts',
  )), false)
  assert.equal(hasOpaqueEmbedSandbox(documentRes(
    200, 'application/javascript', 'sandbox allow-scripts',
  )), true)
  assert.equal(hasOpaqueEmbedSandbox(documentRes(
    200, 'application/javascript', "default-src 'self'",
  )), false)
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

const O = 'https://app.example.com'

test('supersededVersionKeys evicts prior versions of the same route', () => {
  const stored = `${O}/api/apps/7/module?v=200`
  const keys = [
    `${O}/api/apps/7/module?v=100`,   // older version of THIS route → evict
    `${O}/api/apps/7/module?v=150`,   // another older version → evict
    stored,                           // the just-stored key → keep
    `${O}/api/apps/7/frame?v=100`,    // different route type → keep
    `${O}/api/apps/8/module?v=100`,   // different app id → keep
  ]
  const out = supersededVersionKeys(stored, keys)
  assert.deepEqual(out.sort(), [
    `${O}/api/apps/7/module?v=100`,
    `${O}/api/apps/7/module?v=150`,
  ].sort())
})

test('supersededVersionKeys never evicts the stored key itself', () => {
  const stored = `${O}/api/apps/7/module?v=200`
  assert.deepEqual(supersededVersionKeys(stored, [stored]), [])
})

test('supersededVersionKeys treats a missing v as a distinct version', () => {
  // A legacy un-versioned entry for the same route is also superseded.
  const stored = `${O}/api/apps/7/module?v=200`
  assert.deepEqual(
    supersededVersionKeys(stored, [`${O}/api/apps/7/module`]),
    [`${O}/api/apps/7/module`],
  )
  // Conversely, storing the un-versioned key evicts the versioned ones.
  const storedBare = `${O}/api/apps/7/module`
  assert.deepEqual(
    supersededVersionKeys(storedBare, [`${O}/api/apps/7/module?v=200`]),
    [`${O}/api/apps/7/module?v=200`],
  )
})

test('supersededVersionKeys ignores a different origin', () => {
  const stored = `${O}/api/apps/7/module?v=200`
  const other = 'https://evil.example.com/api/apps/7/module?v=100'
  assert.deepEqual(supersededVersionKeys(stored, [other]), [])
})

test('supersededVersionKeys is robust to bad inputs', () => {
  // A non-URL stored key returns nothing rather than throwing.
  assert.deepEqual(supersededVersionKeys('not a url', []), [])
  // A non-URL entry in the list is skipped, not fatal.
  const stored = `${O}/api/apps/7/module?v=200`
  assert.deepEqual(
    supersededVersionKeys(stored, ['::::', `${O}/api/apps/7/module?v=1`]),
    [`${O}/api/apps/7/module?v=1`],
  )
  assert.deepEqual(supersededVersionKeys(stored, undefined), [])
})

test('entriesToTrim returns the oldest keys over the cap', () => {
  const keys = ['a', 'b', 'c', 'd', 'e']
  // Oldest-first (insertion order): trimming to 3 drops the first two.
  assert.deepEqual(entriesToTrim(keys, 3), ['a', 'b'])
})

test('entriesToTrim returns nothing at or under the cap', () => {
  assert.deepEqual(entriesToTrim(['a', 'b'], 3), [])
  assert.deepEqual(entriesToTrim(['a', 'b', 'c'], 3), [])
  assert.deepEqual(entriesToTrim([], 3), [])
})

test('entriesToTrim guards a non-positive cap and missing list', () => {
  // A zero/negative cap is treated as "no trim" rather than wiping everything.
  assert.deepEqual(entriesToTrim(['a', 'b'], 0), [])
  assert.deepEqual(entriesToTrim(['a', 'b'], -1), [])
  assert.deepEqual(entriesToTrim(undefined, 3), [])
})

test('APP_ASSETS_MAX_ENTRIES is a sane positive cap', () => {
  assert.equal(typeof APP_ASSETS_MAX_ENTRIES, 'number')
  assert.ok(APP_ASSETS_MAX_ENTRIES > 0)
})
