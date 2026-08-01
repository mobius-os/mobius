import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import RecoveryLink, {
  RECOVERY_PATH,
} from '../../components/ErrorBoundary/RecoveryLink.jsx'

test('recovery stays a plain last-resort link with adaptable context', () => {
  const defaultHtml = renderToStaticMarkup(createElement(RecoveryLink))
  assert.match(defaultHtml, /class="errbound__recovery"/)
  assert.match(defaultHtml, /If the problem continues after trying again/)
  assert.match(defaultHtml, new RegExp(`href="${RECOVERY_PATH}"`))
  assert.match(defaultHtml, /target="_top"/)

  const standaloneHtml = renderToStaticMarkup(createElement(RecoveryLink, {
    className: 'standalone-app__recovery',
    lead: 'If the app still won’t open,',
  }))
  assert.match(standaloneHtml, /class="standalone-app__recovery"/)
  assert.match(standaloneHtml, /If the app still won’t open/)
  assert.match(standaloneHtml, />open the isolated recovery workspace<\/a>/)
})
