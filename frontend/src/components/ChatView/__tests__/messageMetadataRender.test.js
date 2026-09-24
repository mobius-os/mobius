/* Render the transcript-derived metadata with the real copy control. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
after(() => vite.close())
const { default: MessageMetaRow } = await vite.ssrLoadModule('/src/components/ChatView/MessageMetaRow.jsx')
const { default: useMessageMetadata } = await vite.ssrLoadModule('/src/components/ChatView/hooks/useMessageMetadata.js')

function Metadata({ messages, revealedIndex = -1 }) {
  const metadata = useMessageMetadata(messages)
  return React.createElement('ul', null, messages.map((msg, i) => {
    const { copyText, timestamp, alwaysVisible } = metadata[i]
    return React.createElement('li', { key: i }, React.createElement(MessageMetaRow, {
      copyText, timestamp, role: msg.role, visible: alwaysVisible || revealedIndex === i,
    }))
  }))
}
function rows(messages, revealedIndex) {
  const html = renderToStaticMarkup(React.createElement(Metadata, { messages, revealedIndex }))
  return [...html.matchAll(/<li>(.*?)<\/li>/g)].map(match => match[1])
}
const assistant = content => ({ role: 'assistant', blocks: [{ type: 'text', content }] })
const user = { role: 'user', content: 'Question', ts: '2026-01-01T12:00:00Z' }
const visible = html => html.includes('chat__msg-meta--visible')

test('only the newest settled assistant pins its copy; owner timestamps stay tap-to-reveal', () => {
  const rendered = rows([user, assistant('Old answer'), user, assistant('New answer')])
  assert.deepEqual(rendered.map(visible), [false, false, false, true])
  assert.deepEqual(rows([user, assistant('Answer'), user], 2).map(visible), [false, true, true])
  for (const html of rendered) assert.match(html, /<button[^>]*aria-label="Copy message"/)
  for (const i of [1, 3]) {
    assert.match(rendered[i], /chat__msg-meta--assistant/)
    assert.doesNotMatch(rendered[i], /<time/)
  }
  assert.match(rendered[2], /<time/)
  assert.match(rendered[2], /aria-hidden="true"/)
  assert.match(rendered[3], /aria-hidden="false"/)
})

test('revealing an older assistant does not unpin the latest assistant', () => {
  const messages = [assistant('Old answer'), assistant('Latest answer'), user]
  assert.deepEqual(rows(messages).map(visible), [false, true, false])
  assert.deepEqual(rows(messages, 0).map(visible), [true, true, false])
  assert.deepEqual(rows(messages).map(visible), [false, true, false])
})

test('a newer settled assistant takes the pinned control from its predecessor', () => {
  const messages = [user, assistant('First answer')]
  assert.deepEqual(rows(messages).map(visible), [false, true])
  assert.deepEqual(rows([...messages, assistant('Second answer')]).map(visible), [false, false, true])
})

test('non-copyable assistant rows render no metadata or copy button', () => {
  for (const msg of [
    assistant('   '),
    { role: 'assistant', blocks: [{ type: 'tool', output: 'not prose' }] },
    { ...assistant('internal'), kind: 'compaction' },
    { ...assistant('internal'), kind: 'continuation' },
  ]) {
    assert.deepEqual(rows([msg]), [''])
  }
})
