import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h, Fragment } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { ChatHttpError } from '../sendErrors.js'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { sendFailureNotice } = await vite.ssrLoadModule('/src/components/ChatView/HelperChatSendNotice.jsx')

test('a refused send in a helper chat links back to the parent chat', () => {
  const error = new ChatHttpError(409, { code: 'delegation_managed', parentChatId: 'parent chat/1' })
  const html = render(h(Fragment, null, sendFailureNotice(error)))
  assert.match(html, /^This is a helper’s chat\. Message it from its <a /)
  assert.match(html, /href="\/shell\?chat=parent%20chat%2F1"/)
  assert.match(html, />parent chat<\/a>\.$/)
})

test('without a parent id, or for other failures, the notice stays plain copy', () => {
  assert.equal(
    sendFailureNotice(new ChatHttpError(409, { code: 'delegation_managed' })),
    'This is a helper’s chat. Message it from its parent chat.',
  )
  assert.equal(typeof sendFailureNotice(new ChatHttpError(500)), 'string')
})
