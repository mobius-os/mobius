/* A finished reply owns exactly one References disclosure. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' } }
const vite = await createSsrRenderServer()
const { default: AssistantReply } = await vite.ssrLoadModule(
  '/src/components/ChatView/AssistantReply.jsx',
)
const { assistantReplyGroups } = await vite.ssrLoadModule(
  '/src/components/ChatView/assistantReplies.js',
)
after(() => vite.close())

function searchedMessage(id, url) {
  return {
    role: 'assistant',
    id,
    content: 'Answer',
    blocks: [
      {
        type: 'tool', tool: 'WebSearch', tool_use_id: `${id}-search`, status: 'done',
        sources: [{ url, title: 'Source' }],
      },
      { type: 'text', content: 'Answer' },
    ],
  }
}

// Props as ChatView passes them for a settled reply read from history.
function renderReply(messages, isStreaming = false) {
  const tail = messages.at(-1)
  return renderToStaticMarkup(createElement(AssistantReply, {
    replyGroup: assistantReplyGroups(messages).get(0),
    activeRowIndex: -1,
    activeMirrorMsg: tail,
    activitySourceBlocks: tail.blocks,
    useDbActivePayload: true,
    hasLivePayload: false,
    streamItems: null,
    chatId: 'chat-references',
    isStreaming,
    // A settled answer end: References sit in its footer.
    footer: 'settled',
    stretchSourceRows: [],
  }))
}

function referencesCount(html) {
  return html.match(/class="chat__sources-label"/g)?.length || 0
}

test('a finished reply shows its References once', () => {
  assert.equal(referencesCount(renderReply([searchedMessage('one', 'https://example.com/a')])), 1)
})

test('a reply spanning several messages still shows one References disclosure', () => {
  const html = renderReply([
    searchedMessage('first', 'https://example.com/a'),
    searchedMessage('second', 'https://example.com/b'),
  ])
  assert.equal(referencesCount(html), 1)
})

test('a streaming reply holds its References in place until it settles', () => {
  const html = renderReply([searchedMessage('one', 'https://example.com/a')], true)
  assert.equal(referencesCount(html), 1, 'the line keeps its References, so settling cannot resize it')
  assert.match(html, /<section class="chat__sources chat__sources--held" inert="" aria-hidden="true">/,
    'unseen and unusable until the answer settles')
  assert.doesNotMatch(renderReply([searchedMessage('one', 'https://example.com/a')]), /chat__sources--held/)
})
