// Rejected outbox content remains visible with actions naming only local intent.
import { test, after } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({ appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
after(() => vite.close())
const { default: QueuedMessages } = await vite.ssrLoadModule('/src/components/ChatView/QueuedMessages.jsx')

const rejected = { cid: 'rejected', content: 'Keep my exact offline message', recoveryAvailable: true, dispatchStarted: true }

test('rejected message renders its text, explicit retry, and local-only discard', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items: [rejected], steerActive: true }))
  assert.match(html, /Keep my exact offline message/)
  assert.match(html, /1 needs attention — kept here/)
  assert.match(html, /Send rejected\. Your message is kept here\./)
  assert.match(html, /aria-label="Retry this message"/)
  assert.match(html, /aria-label="Discard local copy of this message"/)
  assert.match(html, /title="Retry keeps the original message unchanged\." disabled=""/)
  assert.doesNotMatch(html, /aria-label="Send this queued message now"/)
  assert.doesNotMatch(html, /aria-label="Cancel queued message"/)
})

test('ordinary pending rows keep the existing edit, cancel, and steer meaning', () => {
  const html = renderToStaticMarkup(createElement(QueuedMessages, { items: [{ ...rejected, recoveryAvailable: false }], steerActive: true }))
  assert.match(html, /aria-label="Cancel queued message"/)
  assert.match(html, /aria-label="Send this queued message now"/)
  assert.doesNotMatch(html, /aria-label="Retry this message"/)
})
