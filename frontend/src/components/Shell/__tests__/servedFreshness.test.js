import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import PlatformDegradedNotice from '../../ErrorBoundary/PlatformDegradedNotice.jsx'

test('the degraded notice names which built-in copy is serving', () => {
  const backend = renderToStaticMarkup(createElement(PlatformDegradedNotice, {
    variant: 'backend', onContinue: () => {},
  }))
  assert.match(backend, /Your latest changes didn’t load/)
  assert.match(backend, /did not load/)

  const frontend = renderToStaticMarkup(createElement(PlatformDegradedNotice, {
    variant: 'frontend', onContinue: () => {},
  }))
  assert.match(frontend, /built-in interface/)
  assert.match(frontend, /did not build/)
  assert.match(frontend, /Continue to the built-in version/)
})
