import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  requestManifestWebInstall,
  resolveInstallManifestUrl,
  supportsWebInstall,
  webInstallPermissionState,
} from '../webInstall.js'

test('detects only callable Web Install implementations', () => {
  assert.equal(supportsWebInstall({ install() {} }), true)
  assert.equal(supportsWebInstall({ install: true }), false)
  assert.equal(supportsWebInstall(null), false)
})

test('resolves a mini-app manifest against the current document', () => {
  assert.equal(
    resolveInstallManifestUrl('/apps/notes/manifest.json', 'https://m.example/shell/'),
    'https://m.example/apps/notes/manifest.json',
  )
})

test('reads the experimental install permission and degrades when unavailable', async () => {
  assert.equal(await webInstallPermissionState({
    permissions: { async query() { return { state: 'denied' } } },
  }), 'denied')
  assert.equal(await webInstallPermissionState({}), 'unknown')
  assert.equal(await webInstallPermissionState({
    permissions: { async query() { throw new TypeError('unknown permission') } },
  }), 'unknown')
})

test('requests direct manifest installation and lets the manifest declare its id', async () => {
  let received = null
  const result = await requestManifestWebInstall({
    manifestUrl: '/apps/notes/manifest.json',
    baseUrl: 'https://m.example/shell/',
    navigatorObject: {
      async install(options) { received = options },
    },
  })

  assert.deepEqual(result, { outcome: 'accepted' })
  assert.deepEqual(received, {
    manifest: 'https://m.example/apps/notes/manifest.json',
  })
})

test('separates cancellation, technical failure, and unsupported browsers', async () => {
  const cancelled = await requestManifestWebInstall({
    manifestUrl: '/manifest.json',
    baseUrl: 'https://m.example/',
    navigatorObject: {
      async install() { throw Object.assign(new Error('no'), { name: 'AbortError' }) },
    },
  })
  const failed = await requestManifestWebInstall({
    manifestUrl: '/manifest.json',
    baseUrl: 'https://m.example/',
    navigatorObject: {
      async install() { throw Object.assign(new Error('bad'), { name: 'DataError' }) },
    },
  })
  const unsupported = await requestManifestWebInstall({
    manifestUrl: '/manifest.json',
    baseUrl: 'https://m.example/',
    navigatorObject: {},
  })

  assert.deepEqual(cancelled, { outcome: 'dismissed' })
  assert.deepEqual(failed, { outcome: 'failed', errorName: 'DataError' })
  assert.deepEqual(unsupported, { outcome: 'unsupported' })
})

test('a blocked host skips install and a newly denied prompt redirects', async () => {
  let blockedCalls = 0
  const blocked = await requestManifestWebInstall({
    manifestUrl: '/manifest.json',
    baseUrl: 'https://m.example/',
    permissionState: 'denied',
    navigatorObject: {
      async install() { blockedCalls += 1 },
    },
  })
  assert.deepEqual(blocked, { outcome: 'blocked' })
  assert.equal(blockedCalls, 0)

  let permissionReads = 0
  const newlyDenied = await requestManifestWebInstall({
    manifestUrl: '/manifest.json',
    baseUrl: 'https://m.example/',
    navigatorObject: {
      async install() {
        throw Object.assign(new Error('permission declined'), { name: 'AbortError' })
      },
      permissions: {
        async query() {
          permissionReads += 1
          return { state: 'denied' }
        },
      },
    },
  })
  assert.deepEqual(newlyDenied, { outcome: 'blocked' })
  assert.equal(permissionReads, 1)
})
