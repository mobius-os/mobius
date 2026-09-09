/* Result disclosures preserve text and never imply permission to resume. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Card } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const { _resetDisclosureStateForTests, persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
after(() => vite.close())
function render(extra = {}, open = true) {
  _resetDisclosureStateForTests()
  if (open) persistDisclosureOpen('chat', 'delegation:one:completed', true)
  return renderToStaticMarkup(React.createElement(Card, { chatId: 'chat', event: {
    id: 'delegation:one:completed', status: 'completed', task_key: 'Review', created_at: 2000,
    body: '<script>untrusted result</script>\nSecond line', consumption: 'available', ...extra,
  } }))
}
test('collapsed helper result is a timestamped disclosure, not another message', () => {
  const html = render({}, false)
  assert.match(html, /Helper finished · Review/)
  assert.match(html, /aria-expanded="false"/)
  assert.match(html, /<time/)
  assert.doesNotMatch(html, /untrusted result/)
})
test('opening results preserves safe full text without claiming agent consumption', () => {
  const html = render()
  assert.match(html, /opening this does not resume work/)
  assert.match(html, /&lt;script&gt;untrusted result&lt;\/script&gt;\nSecond line/)
  assert.doesNotMatch(html, /Incorporated by the agent/)
})
test('incorporation is explicit evidence, not inferred from rendering', () => {
  assert.match(render({ consumption: 'incorporated' }), /Incorporated by the agent/)
})
test('failure and truncated results remain inspectable in place', () => {
  const html = render({ status: 'failed', result_truncated: true, child_chat_id: 'helper' })
  assert.match(html, /Helper failed/)
  assert.match(html, /Excerpt/)
  assert.match(html, /href="\/chat\/helper"/)
})
