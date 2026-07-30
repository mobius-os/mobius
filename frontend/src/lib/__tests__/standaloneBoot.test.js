import test from 'node:test'
import assert from 'node:assert/strict'

import {
  initiallyOpenStandaloneInstallCard,
  isVisualContentOnly,
  readStandaloneBoot,
  standaloneAppVersion,
  standaloneInstallCompleted,
} from '../standaloneBoot.js'

function docWith(text) {
  return { getElementById: () => ({ textContent: text }) }
}

test('standalone boot requires a server-authored complete app identity', () => {
  assert.equal(readStandaloneBoot(docWith('')), null)
  assert.equal(readStandaloneBoot(docWith('{')), null)
  assert.equal(readStandaloneBoot(docWith('{"id":7,"slug":"notes"}')), null)
  assert.deepEqual(
    readStandaloneBoot(docWith('{"id":7,"slug":"notes","name":"Notes"}')),
    { id: 7, slug: 'notes', name: 'Notes' },
  )
})

test('standalone app version follows executable updated_at', () => {
  assert.equal(standaloneAppVersion({ updated_at: '2026-07-30T01:00:00' }), '2026-07-30T01:00:00')
  assert.equal(standaloneAppVersion({}), '0')
})

test('an installed PWA stays quiet on launch but shows success after installation', () => {
  assert.equal(initiallyOpenStandaloneInstallCard({
    installState: 'installed', forceOpen: false, dismissed: false,
  }), false)
  assert.equal(initiallyOpenStandaloneInstallCard({
    installState: 'installed', forceOpen: true, dismissed: false,
  }), true)
  assert.equal(initiallyOpenStandaloneInstallCard({
    installState: 'manual', forceOpen: false, dismissed: false,
  }), true)
  assert.equal(standaloneInstallCompleted('ready', 'installed'), true)
  assert.equal(standaloneInstallCompleted('installed', 'installed'), false)
})

test('standalone visual capture suppresses host overlays without touching app DOM', () => {
  assert.equal(isVisualContentOnly({ getItem: () => '1' }), true)
  assert.equal(isVisualContentOnly({ getItem: () => null }), false)
  assert.equal(isVisualContentOnly({ getItem: () => { throw new Error('blocked') } }), false)
})
