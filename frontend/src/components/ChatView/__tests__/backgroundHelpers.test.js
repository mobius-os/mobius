import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import WaitingChip from '../WaitingChip.jsx'
import { chatHasSelfResumingHandoff } from '../chatRuntimeCache.js'

test('idle waking helpers own one visible automatic Waiting handoff', () => {
  const helpers = {
    count: 2,
    items: [{ task_key: 'review_copy' }, { task_key: 'verify-build' }],
  }
  assert.equal(chatHasSelfResumingHandoff({ backgroundHelpers: helpers }), true)
  const html = renderToStaticMarkup(createElement(WaitingChip, {
    backgroundHelpers: helpers,
  }))
  assert.match(html, /Waiting on 2 helpers/)
  assert.match(html, /resumes automatically/)
  assert.match(html, /Review copy, Verify build/)
})

test('live parent work suppresses the helper handoff without erasing it', () => {
  const backgroundHelpers = { count: 1, items: [] }
  assert.equal(chatHasSelfResumingHandoff({ backgroundHelpers }), true)
  assert.equal(chatHasSelfResumingHandoff({
    turnActive: true,
    backgroundHelpers,
  }), false)
})
