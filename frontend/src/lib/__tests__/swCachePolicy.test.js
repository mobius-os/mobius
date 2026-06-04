import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ESM_CACHE,
  OFFLINE_APPS_CACHE,
  STANDALONE_APPS_CACHE,
  VENDOR_CACHE,
  isStaleRuntimeCache,
} from '../../sw-cache-policy.js'

test('runtime cache cleanup keeps current cache names', () => {
  for (const name of [
    VENDOR_CACHE,
    ESM_CACHE,
    OFFLINE_APPS_CACHE,
    STANDALONE_APPS_CACHE,
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
