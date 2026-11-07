/** Render the real Settings update view to pin the "a newer release waits
 *  behind the update in flight" suggestion and its cancel-and-update-to-latest
 *  action, which never switches releases by itself. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { PlatformUpdatesView } = await vite.ssrLoadModule('/src/components/SettingsView/PlatformUpdates.jsx')
after(() => vite.close())

const target = 'c'.repeat(40)

function render(platform) {
  const update = {
    platform, cachedPlatform: null, rebuild: null, version: null, phase: 'idle',
    busy: false, reconnecting: false, error: '', errorCode: '', checkResult: '',
  }
  return renderToStaticMarkup(createElement(PlatformUpdatesView, { update, onOpenChat: () => {} }))
}

test('a cancellable in-flight update suggests the newer release and offers to jump to it', () => {
  const html = render({
    state: 'activation_needed', contained_upstream_sha: target,
    // A release newer than the pinned one is never contained yet.
    available: true, newer_updates_available: true,
    activation: { level: 'image_rebuild', required_actions: ['image_rebuild'] },
    unfinished_update: { stage: 'finish', action: 'replace', cancellable: true },
  })
  assert.match(html, /A version newer than the update in progress is available\./)
  assert.match(html, /<button[^>]*>Cancel and update to latest<\/button>/)
  // Only a suggestion: the primary action still finishes this update.
  assert.match(html, /<button[^>]*>Finish update<\/button>/)
})

test('a settling update names the newer release but offers no cancel-to-latest', () => {
  const html = render({
    state: 'up_to_date', contained_upstream_sha: target,
    // A release newer than the pinned one is never contained yet.
    available: true, newer_updates_available: true,
    unfinished_update: { stage: 'settling', action: 'none', cancellable: false },
  })
  assert.match(html, /It will be offered once this update finishes\./)
  assert.doesNotMatch(html, /Cancel and update to latest/)
  // Confirming the settling update stays the primary action; nothing is reviewed.
  assert.match(html, /<button[^>]*>Check again<\/button>/)
  assert.doesNotMatch(html, /Review update/)
})

test('an ordinary available update with nothing in flight shows no in-flight suggestion', () => {
  const html = render({
    state: 'available', available: true, contained_upstream_sha: target,
    newer_updates_available: false, unfinished_update: null,
  })
  assert.doesNotMatch(html, /than the update in progress/)
  assert.doesNotMatch(html, /Cancel and update to latest/)
})
