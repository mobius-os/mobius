import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import RecoveryLink, {
  RECOVERY_CONTROL_URL,
} from '../../components/ErrorBoundary/RecoveryLink.jsx'

function renderLink(props = {}) {
  return renderToStaticMarkup(createElement(RecoveryLink, props))
}

test('the recovery link always offers both external recovery routes', () => {
  assert.equal(RECOVERY_CONTROL_URL, 'https://www.mobius.you/')
  const html = renderLink()
  assert.match(html, /class="errbound__recovery"/)
  assert.match(html, /If the problem continues after trying again/)
  assert.match(html, /Managed hosting:/)
  assert.match(html, new RegExp(`href="${RECOVERY_CONTROL_URL}"`))
  assert.match(html, /target="_top"/)
  assert.match(html, />open Recovery in mobius\.you<\/a>/)
  assert.match(html, /Self-hosted — run this on the server:/)
  assert.match(html, /<code[^>]*>mobiusctl recovery<\/code>/)
  // It is a fallback for a surface that already failed, so it must never point
  // back at an in-app recovery route.
  assert.doesNotMatch(html, /href="\/recover/)
})

test('the link takes its host surface class and lead', () => {
  const html = renderLink({
    className: 'standalone-app__recovery',
    lead: 'If the app still won’t open,',
  })
  assert.match(html, /class="standalone-app__recovery"/)
  assert.match(html, /If the app still won’t open/)
  assert.match(html, /Managed hosting:/)
  assert.match(html, /<code[^>]*>mobiusctl recovery<\/code>/)
})
