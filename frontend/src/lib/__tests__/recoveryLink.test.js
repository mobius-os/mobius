import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import RecoveryLink, {
  RECOVERY_CONTROL_URL,
} from '../../components/ErrorBoundary/RecoveryLink.jsx'

test('recovery points outside the Mobius container with self-host guidance', () => {
  assert.equal(RECOVERY_CONTROL_URL, 'https://www.mobius.you/')
  const defaultHtml = renderToStaticMarkup(createElement(RecoveryLink))
  assert.match(defaultHtml, /class="errbound__recovery"/)
  assert.match(defaultHtml, /If the problem continues after trying again/)
  assert.match(defaultHtml, new RegExp(`href="${RECOVERY_CONTROL_URL}"`))
  assert.match(defaultHtml, /target="_top"/)
  assert.match(defaultHtml, /mobiusctl recovery start/)
  assert.doesNotMatch(defaultHtml, /href="\/recover/)

  const selfHostedHtml = renderToStaticMarkup(createElement(RecoveryLink, {
    deployment: 'self_hosted',
  }))
  assert.match(selfHostedHtml, /This is a self-hosted Möbius instance/)
  assert.match(selfHostedHtml, /mobiusctl recovery start/)
  assert.doesNotMatch(selfHostedHtml, /mobius\.you/)

  const railwayHtml = renderToStaticMarkup(createElement(RecoveryLink, {
    deployment: 'railway',
  }))
  assert.match(railwayHtml, /managed on Railway/)
  assert.match(railwayHtml, />Open Recovery in mobius\.you<\/a>/)
  assert.doesNotMatch(railwayHtml, /mobiusctl recovery start/)

  const standaloneHtml = renderToStaticMarkup(createElement(RecoveryLink, {
    className: 'standalone-app__recovery',
    lead: 'If the app still won’t open,',
  }))
  assert.match(standaloneHtml, /class="standalone-app__recovery"/)
  assert.match(standaloneHtml, /If the app still won’t open/)
  assert.match(standaloneHtml, />open Recovery in mobius\.you<\/a>/)
})
