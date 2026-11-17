// A `mobius-key-request` fence renders the live key request card in the chat
// whose agent asked: the same two-step card as over an app, asking first and
// taking a new key's value only after Continue. In another chat the card is
// read-only and points to the chat that asked, and only assistant replies
// turn the fence into a card.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KeyRequestBlock, { KeyAnswerLine } from '../markdown/KeyRequestBlock.jsx'
import KeyRequest from '../../SettingsView/KeyRequest.jsx'
import { BlockToken } from '../markdown/blocks.jsx'
import { AppBlockChatContext, createAppBlockChat } from '../markdown/appBlockChat.js'
import { KEYS_QUERY } from '../../SettingsView/keysQueries.js'
import { appQueries } from '../../../hooks/queries.js'

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const ask = {
  id: 'agent-maps', requester: 'agent', app: null, app_name: 'Your agent', key: 'maps', reason: 'Fetch map tiles',
  sites: [TILES], inject: null, status: 'pending', ticket: 't1', chat_id: 'chat-1', key_exists: false, granted: false,
}

function render(element, { chatId = 'chat-1', requests = [ask], keys = [], usage = null } = {}) {
  const client = new QueryClient()
  client.setQueryData(appQueries.list.key, [{ id: 7, name: 'Weather' }])
  client.setQueryData(KEYS_QUERY, { available: true, keys, requests })
  if (usage) client.setQueryData([...KEYS_QUERY, 'usage', 'maps'], { usage })
  const inChat = createElement(AppBlockChatContext.Provider, { value: createAppBlockChat(chatId) }, element)
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, inChat))
}

test("the asking chat asks first, then takes a new key's value after Continue", () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }))
  assert.match(html, /<span>Let <\/span><strong>your agent<\/strong><span> send <\/span><strong>maps<\/strong>/)
  assert.match(html, /Your agent says:<\/span> Fetch map tiles/)
  // The value is the second step, never asked next to the question.
  assert.doesNotMatch(html, /type="password"|>Allow</)
  assert.match(html, />Not now<\/button>/)
  assert.match(html, />Continue<\/button>/)
})

test("another chat's card shows the request read-only and points to the chat that asked", () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }), { chatId: 'chat-2' })
  assert.match(html, /<strong>maps<\/strong>/)
  assert.doesNotMatch(html, /type="password"|>Allow<|>Not now<|>Continue</)
  assert.match(html, /Another chat asked for this\. <a[^>]*href="\/shell\/\?chat=chat-1"[^>]*>Answer it there<\/a>/)
  assert.doesNotMatch(html, /settings=keys/)
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

// -- answering at a glance is not possible -----------------------------------

const existing = { ...ask, key_exists: true }

test('Not now and Allow start asleep on every card, so a card appearing under the pointer takes no click', () => {
  for (const html of [
    render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }), { requests: [existing] }),
    render(createElement(KeyRequest, { request: existing, names: {}, onAnswered() {} })),
  ]) {
    // aria-disabled, not disabled: an asleep answer still hears the presses that keep it asleep.
    assert.match(html, /<button[^>]*aria-disabled="true"[^>]*>Not now<\/button>/)
    assert.match(html, /<button[^>]*aria-disabled="true"[^>]*>Allow<\/button>/)
    assert.doesNotMatch(html, /<button[^>]* disabled=""/)
  }
})

// -- an app's request in a chat ----------------------------------------------

const appRequest = { ...ask, id: '7-maps', requester: 'app', app: 7, app_name: 'Weather', chat_id: null }

test("an app's request in a chat is shown read-only and says the app asks when opened", () => {
  const html = render(createElement(KeyRequestBlock, { block: { id: '7-maps' } }), { requests: [appRequest] })
  assert.match(html, /<strong>Weather<\/strong>/)
  assert.doesNotMatch(html, /type="password"|>Allow<|>Not now<|>Continue<|Use .*'s key/)
  assert.match(html, /Weather asked for this\. It asks again when you open <a[^>]*href="\/shell\/\?app=7"[^>]*>Weather<\/a>/)
  assert.doesNotMatch(html, /settings=keys/)
})

// -- after an answer ---------------------------------------------------------

test('an answered card says what the owner decided, and if the agent could not be told, how to carry on', () => {
  const told = renderToStaticMarkup(createElement(KeyAnswerLine, { request: ask, kind: 'approve', names: {}, toldAgent: true }))
  assert.match(told, /You allowed your agent to send maps to tiles\.example\.com\./)
  assert.doesNotMatch(told, /Couldn&#x27;t tell your agent/)
  const untold = renderToStaticMarkup(createElement(KeyAnswerLine, { request: ask, kind: 'deny', names: {}, toldAgent: false }))
  assert.match(untold, /You said no to your agent sending maps to tiles\.example\.com\./)
  assert.match(untold, /Couldn&#x27;t tell your agent\. Send it a message to carry on\./)
})

test("after a reload an allowed request that left the vault says what was allowed, from the key's activity", () => {
  const keys = [{ name: 'maps', grants: { agents: true, sites: [TILES], apps: [] } }]
  const usage = [
    { key: 'maps', caller: 'owner', outcome: 'granted agents', ts: '2026-11-16T10:00:00Z' },
    { key: 'maps', caller: 'agent:chat-1', outcome: 'requested', ts: '2026-11-16T09:59:00Z' },
  ]
  const html = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }), { requests: [], keys, usage })
  assert.match(html, /You allowed your agent to send maps to tiles\.example\.com\./)
  assert.doesNotMatch(html, /answered or withdrawn/)
})

