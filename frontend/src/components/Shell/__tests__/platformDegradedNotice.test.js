import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import PlatformDegradedNotice from '../../ErrorBoundary/PlatformDegradedNotice.jsx'

test('the degraded notice offers agent repair or the previous version directly', () => {
  const backend = renderToStaticMarkup(createElement(PlatformDegradedNotice, {
    variant: 'backend', onContinue: () => {},
  }))
  assert.match(backend, /Your latest changes didn’t load/)
  assert.match(backend, /Fix with an agent/)
  assert.match(backend, /Recommended/)
  assert.match(backend, /Use previous version/)
  assert.doesNotMatch(backend, />Refresh</)
  assert.doesNotMatch(backend, /Built-in version · Repair/)

  const frontend = renderToStaticMarkup(createElement(PlatformDegradedNotice, {
    variant: 'frontend', onContinue: () => {},
  }))
  assert.match(frontend, /latest interface didn’t load/)
  assert.match(frontend, /did not build/)
  assert.match(frontend, /Use previous version/)
})
