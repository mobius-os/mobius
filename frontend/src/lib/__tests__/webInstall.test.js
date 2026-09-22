import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  preferredDirectInstallMode,
  requestManifestWebInstall,
  resolveInstallManifestUrl,
  supportsManifestInstallElement,
  supportsWebInstall,
} from '../webInstall.js'

test('detects only callable Web Install implementations', () => {
  assert.equal(supportsWebInstall({ install() {} }), true)
  assert.equal(supportsWebInstall({ install: true }), false)
  assert.equal(supportsWebInstall(null), false)
})

test('detects the current manifest-based install element, not the retired design', () => {
  class CurrentInstallElement {}
  CurrentInstallElement.prototype.manifest = ''
  class OldInstallElement {}
  OldInstallElement.prototype.installurl = ''

  assert.equal(supportsManifestInstallElement({
    HTMLInstallElement: CurrentInstallElement,
  }), true)
  assert.equal(supportsManifestInstallElement({
    HTMLInstallElement: OldInstallElement,
  }), false)
  assert.equal(supportsManifestInstallElement({}), false)
})

test('prefers the trusted install element, then the imperative API', () => {
  class CurrentInstallElement {}
  CurrentInstallElement.prototype.manifest = ''

  assert.equal(preferredDirectInstallMode({
    windowObject: { HTMLInstallElement: CurrentInstallElement },
    navigatorObject: { install() {} },
  }), 'element')
  assert.equal(preferredDirectInstallMode({
    windowObject: {},
    navigatorObject: { install() {} },
  }), 'api')
  assert.equal(preferredDirectInstallMode({
    windowObject: {},
    navigatorObject: {},
  }), null)
})

test('resolves a mini-app manifest against the current document', () => {
  assert.equal(
    resolveInstallManifestUrl('/apps/notes/manifest.json', 'https://m.example/shell/'),
    'https://m.example/apps/notes/manifest.json',
  )
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
