// A `mobius-key-request` fence renders the live key request card in the chat
// whose agent asked: the same card as Settings › Keys, with the sealed paste
// field for a new key. In another chat the card is read-only and points to
// Settings › Keys, and only assistant replies turn the fence into a card.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KeyRequestBlock from '../markdown/KeyRequestBlock.jsx'
import { BlockToken } from '../markdown/blocks.jsx'
import { AppBlockChatContext, createAppBlockChat } from '../markdown/appBlockChat.js'
import { KEYS_QUERY } from '../../SettingsView/keysQueries.js'
import { appQueries } from '../../../hooks/queries.js'

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const ask = {
  id: 'agent-maps', requester: 'agent', app: null, app_name: 'Your agent', key: 'maps', reason: 'Fetch map tiles',
  sites: [TILES], inject: null, status: 'pending', ticket: 't1', chat_id: 'chat-1', key_exists: false, granted: false,
}

function render(element, { chatId = 'chat-1', requests = [ask] } = {}) {
  const client = new QueryClient()
  client.setQueryData(appQueries.list.key, [])
  client.setQueryData(KEYS_QUERY, { available: true, keys: [], requests })
  const inChat = createElement(AppBlockChatContext.Provider, { value: createAppBlockChat(chatId) }, element)
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, inChat))
}

test("the asking chat shows the request card with a sealed paste field and the owner's answers", () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }))
  assert.match(html, /<strong>Your agent<\/strong><span> wants to send <\/span><strong>maps<\/strong>/)
  assert.match(html, /<input[^>]*type="password"[^>]*aria-label="Key value"/)
  assert.match(html, />Not now<\/button>/)
  assert.match(html, />Allow<\/button>/)
})

test("another chat's card shows the request read-only and points to Settings › Keys", () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }), { chatId: 'chat-2' })
  assert.match(html, /wants to send/)
  assert.doesNotMatch(html, /type="password"|>Allow<|>Not now</)
  assert.match(html, /href="\/shell\/\?settings=keys"[^>]*>Settings › Keys<\/a>/)
})

test('a request no longer waiting says it was answered or withdrawn', () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }), { requests: [] })
  assert.match(html, /This request was answered or withdrawn\./)
  assert.doesNotMatch(html, /<input|<button/)
})

const token = { type: 'code', lang: 'mobius-key-request', text: '{"id":"agent-maps"}', raw: '```mobius-key-request\n{"id":"agent-maps"}\n```' }

test("only an assistant reply turns the fence into a card; elsewhere it stays code", () => {
  assert.match(render(createElement(BlockToken, { token, allowAppBlocks: true })), /key-request-place/)
  const plain = render(createElement(BlockToken, { token, allowAppBlocks: false }))
  assert.doesNotMatch(plain, /key-request-place/)
  assert.match(plain, /agent-maps/)
})
