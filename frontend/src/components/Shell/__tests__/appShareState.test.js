import assert from 'node:assert/strict'
import test from 'node:test'

import {
  appInstallManifestUrl,
  appInstallShareText,
  appNativeSharePayload,
  appShareState,
  isDrawerAppShareEligible,
} from '../../Drawer/appShareState.js'

test('typed distribution manifest publishes a local app without exposing storage fields', () => {
  const app = {
    name: 'Published later',
    distribution_manifest: {
      kind: 'published',
      url: 'https://raw.example/published-later/mobius.json',
    },
  }
  assert.equal(appInstallManifestUrl(app), app.distribution_manifest.url)
  assert.deepEqual(appShareState(app, []), {
    kind: 'published',
    installUrl: app.distribution_manifest.url,
  })
})

test('every installed app can expose its independent public-use control', () => {
  const app = {
    name: 'News',
    distribution_manifest: {
      kind: 'source',
      url: 'https://raw.example/news/mobius.json',
    },
  }
  assert.equal(isDrawerAppShareEligible(app), true)
  assert.equal(appInstallManifestUrl(app), app.distribution_manifest.url)
  assert.equal(isDrawerAppShareEligible({ distribution_manifest: null }), true)
  assert.equal(
    isDrawerAppShareEligible({ distribution_manifest: { url: 'https://raw.example/app' } }),
    true,
  )
  assert.equal(isDrawerAppShareEligible(null), false)
})

test('local apps route through installed Contribute before the App Store', () => {
  const apps = [
    { id: 39, slug: 'app-store' },
    { id: 80, slug: 'contribute' },
  ]
  assert.deepEqual(appShareState({ name: 'Local' }, apps), {
    kind: 'open-contribute',
    targetApp: apps[1],
  })
})

test('local apps offer the App Store when Contribute is absent', () => {
  const store = { id: 39, slug: 'app-store' }
  assert.deepEqual(appShareState({ name: 'Local' }, [store]), {
    kind: 'install-contribute',
    targetApp: store,
  })
  assert.deepEqual(appShareState({ name: 'Local' }, []), {
    kind: 'unavailable',
    targetApp: null,
  })
})

test('share payloads carry install instructions and one public URL', () => {
  const app = { name: 'Published later' }
  const url = 'https://raw.example/published-later/mobius.json'
  assert.equal(
    appInstallShareText(app, url),
    'Install Published later in Möbius:\n' + url +
      '\n\nOpen App Store → From URL, paste this link, then review and install.',
  )
  assert.deepEqual(appNativeSharePayload(app, url), {
    title: 'Published later',
    text: 'Install Published later in Möbius. Open App Store → From URL.',
    url,
  })
})
