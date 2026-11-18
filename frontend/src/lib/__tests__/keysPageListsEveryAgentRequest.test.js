// Settings › Keys lists every request the owner's agents made, under the chat
// that asked, so none is ever stranded: not when the turn was stopped and its
// card settled, not when its chat was deleted, and not when its card was
// never saved. An app's request is answered over the app's own window.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { KeysPage, RequestChat } from '../../components/SettingsView/KeysSection.jsx'
import { KEYS_QUERY } from '../../components/SettingsView/keysQueries.js'
import { settingsKeyRequests } from '../keyRequestPlaces.js'

const SITE = { host: 'api.example.com', methods: ['GET'], paths: ['/*'] }
const ask = (id, extra = {}) => ({
  id, key: id.replace(/^agent-/, ''), requester: 'agent', app: null, app_name: 'Your agent',
  reason: 'Fetch the forecast', sites: [SITE], inject: null, status: 'pending', ticket: 't',
  created_at: '2026-11-17T20:00:00Z', key_exists: false, key_origin: null, granted: false,
  sets_inject: false, inject_kept: false, key_inject: null, chat_id: null, ...extra,
})
const REQUESTS = [
  ask('agent-weather', { chat_id: 'chat-1', chat_title: 'Weather chat' }),
  ask('agent-tiles', { chat_id: 'chat-2', chat_title: null }),
  ask('agent-old'),
  ask('agent-no', { status: 'denied', chat_id: 'chat-1', chat_title: 'Weather chat' }),
  { ...ask('7-maps'), requester: 'app', app: 7, app_name: 'Maps' },
]

function keysPage() {
  const client = new QueryClient()
  client.setQueryData(KEYS_QUERY, { available: true, keys: [], requests: REQUESTS })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client },
    createElement(KeysPage, { onOpenKey() {}, onAddKey() {}, onOpenChat() {} })))
}

test('every waiting agent request is listed, and no app request or refusal', () => {
  assert.deepEqual(settingsKeyRequests({ requests: REQUESTS }).map(r => r.id), ['agent-weather', 'agent-tiles', 'agent-old'])
  assert.deepEqual(settingsKeyRequests(undefined), [])
  const page = keysPage()
  assert.match(page, /Waiting for you/)
  assert.match(page, /Answer here, or on the card in the chat that asked/)
})

test('each request says which chat asked, and a deleted chat as such', () => {
  const line = request => renderToStaticMarkup(createElement(RequestChat, { request, onOpenChat() {} }))
  assert.match(line(REQUESTS[0]), /Asked in “Weather chat”/)
  assert.match(line(REQUESTS[0]), /Open the chat/)
  assert.match(line(REQUESTS[1]), /a chat that has since been deleted/)
  assert.doesNotMatch(line(REQUESTS[1]), /Open the chat/)
  assert.equal(line(REQUESTS[2]), '')
  const page = keysPage()
  assert.match(page, /Asked in “Weather chat”/)
  assert.match(page, /since been deleted/)
})
