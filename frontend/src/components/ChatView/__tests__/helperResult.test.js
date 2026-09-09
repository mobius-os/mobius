/* Result disclosures preserve text and never imply permission to resume. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Card } = await vite.ssrLoadModule('/src/components/ChatView/HelperResultCard.jsx')
const { _resetDisclosureStateForTests, persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
const priorWindow = globalThis.window
globalThis.window = { location: new URL('https://mobius.test/shell') }
after(() => { globalThis.window = priorWindow; return vite.close() })
function render(extra = {}, open = true) {
  _resetDisclosureStateForTests()
  if (open) persistDisclosureOpen('chat', 'delegation:one:completed', true)
  return renderToStaticMarkup(React.createElement(Card, { chatId: 'chat', event: {
    id: 'delegation:one:completed', status: 'completed', task_key: 'Review', created_at: 2000,
    body: '<script>untrusted result</script>\n\nSecond line', consumption: 'available', ...extra,
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
  assert.doesNotMatch(html, /<script|untrusted result/)
  assert.match(html, /Second line/)
  assert.doesNotMatch(html, /Incorporated by the agent/)
})
test('incorporation is explicit evidence, not inferred from rendering', () => {
  assert.match(render({ consumption: 'incorporated' }), /Incorporated by the agent/)
})
test('unknown and notification-only history use neutral delivery copy', () => {
  const unknown = render({ consumption: 'unknown' })
  assert.match(unknown, /Agent incorporation is not known/)
  assert.doesNotMatch(unknown, /Available to the agent|Incorporated by the agent/)
  const notified = render({ consumption: 'notified' })
  assert.match(notified, /Delivery recorded · agent incorporation is not known/)
  assert.doesNotMatch(notified, /Available to the agent|Incorporated by the agent/)
})
test('failure and truncated results remain inspectable in place', () => {
  const html = render({ status: 'failed', result_truncated: true, child_chat_id: 'helper' })
  assert.match(html, /Helper failed/)
  assert.match(html, /Excerpt/)
  assert.match(html, /href="\/shell\?chat=helper"/)
})

test('helper disclosure uses the same quiet chrome and structured prose as peer activity', () => {
  const html = render({ body: '## Review\n\n**Passed**\n\n- One outcome\n- No automatic resume' })
  assert.equal((html.match(/<svg/g) || []).length, 1, 'only the direction icon, no trailing disclosure chevron')
  assert.match(html, /<h2[^>]*>Review<\/h2>/)
  assert.match(html, /<strong>Passed<\/strong>/)
  assert.match(html, /<ul/)
  assert.match(html, /opening this does not resume work/)
})
