import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import StandaloneInstallCard from '../../components/StandaloneApp/StandaloneInstallCard.jsx'

// Fresh module state on purpose (separate file = separate process):
// `installPrompt` never saw a capture start, so the snapshot is `manual` —
// exactly the state a fresh Chromium arrival lands in while the browser's
// engagement heuristics still gate `beforeinstallprompt`.

function withNavigator(userAgent, run) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: { userAgent, maxTouchPoints: 0 },
  })
  try {
    return run()
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'navigator', descriptor)
    else delete globalThis.navigator
  }
}

const app = { slug: 'notes', name: 'Notes', updated_at: '1' }

test('Chromium arrival in manual state warms up instead of instructing', () => {
  const html = withNavigator(
    'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36',
    () => renderToStaticMarkup(
      createElement(StandaloneInstallCard, { app, forceOpen: true }),
    ),
  )
  assert.match(html, /standalone-install__warming/)
  assert.match(html, /Show the manual steps/)
  // No menu instructions yet, and no duplicate "Show me" action while the
  // warming skip button is on screen.
  assert.doesNotMatch(html, /standalone-install__instructions/)
  assert.doesNotMatch(html, /standalone-install__actions/)
})

test('a browser with no install prompt gets instructions immediately', () => {
  const html = withNavigator(
    'Mozilla/5.0 (Android 14; Mobile; rv:121.0) Gecko/121.0 Firefox/121.0',
    () => renderToStaticMarkup(
      createElement(StandaloneInstallCard, { app, forceOpen: true }),
    ),
  )
  assert.doesNotMatch(html, /standalone-install__warming/)
  assert.match(html, /standalone-install__instructions/)
})
