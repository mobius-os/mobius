import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ShellFreshnessNotice from '../../ErrorBoundary/ShellFreshnessNotice.jsx'
import PlatformDegradedNotice from '../../ErrorBoundary/PlatformDegradedNotice.jsx'

function renderFreshness(version) {
  const client = new QueryClient()
  return renderToStaticMarkup(createElement(
    QueryClientProvider, { client },
    createElement(ShellFreshnessNotice, { version }),
  ))
}

const fresh = {
  served_frontend: 'abc123',
  frontend_source: 'platform',
  frontend_stale: false,
  frontend_building: false,
  frontend_build_error: null,
}

test('a fresh or unknown served shell shows nothing', () => {
  assert.equal(renderFreshness(fresh), '')
  assert.equal(renderFreshness(undefined), '')
  // The baked shell is the degraded notice's case, not this strip's.
  assert.equal(
    renderFreshness({ ...fresh, frontend_source: 'baked', frontend_stale: true }),
    '',
  )
})

test('a served shell behind the source says so, and whether a rebuild is under way', () => {
  const behind = renderFreshness({ ...fresh, frontend_stale: true })
  assert.match(behind, /behind your latest changes/)
  assert.match(behind, /Ask the agent to fix it/)
  assert.match(behind, /role="status"/)

  const building = renderFreshness({
    ...fresh, frontend_stale: true, frontend_building: true,
  })
  assert.match(building, /Rebuilding the interface with your latest changes/)
  assert.doesNotMatch(building, /Ask the agent/)

  const failed = renderFreshness({
    ...fresh, frontend_stale: true, frontend_build_error: 'vite: out of memory',
  })
  assert.match(failed, /couldn’t rebuild with your latest changes/)
  assert.match(failed, /vite: out of memory/)
  assert.match(failed, /Ask the agent to fix it/)
})

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
