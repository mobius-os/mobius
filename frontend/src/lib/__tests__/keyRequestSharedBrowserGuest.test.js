// A shared-browser guest never reads or answers the owner's key requests:
// the vault's routes refuse them, so every place a card shows says plainly
// that only the owner answers, rather than going blank. This file signs the
// whole test process in as a guest.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { beginSharedBrowserAuth } from '../../api/client.js'
import AppKeyRequestCard from '../../components/AppCanvas/AppKeyRequestCard.jsx'
import KeyRequestBlock from '../../components/ChatView/markdown/KeyRequestBlock.jsx'
import KeysSummary, { KeyPage, KeysPage } from '../../components/SettingsView/KeysSection.jsx'
import { KEYS_QUERY } from '../../components/SettingsView/keysQueries.js'
import { appQueries } from '../../hooks/queries.js'

beginSharedBrowserAuth()

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const pending = {
  id: '7-maps', requester: 'app', app: 7, app_name: 'Weather', key: 'maps', status: 'pending', ticket: 't1',
  sites: [TILES], inject: null, chat_id: null, key_exists: true, granted: false,
}

// Even with the owner's requests in the cache, a guest sees none of them.
function render(element) {
  const client = new QueryClient()
  client.setQueryData(appQueries.list.key, [{ id: 7, name: 'Weather' }])
  client.setQueryData(KEYS_QUERY, { available: true, keys: [{ name: 'maps', grants: {} }], requests: [pending] })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, element))
}

test("a guest never sees the card over an app", () => {
  assert.equal(render(createElement(AppKeyRequestCard, { appId: 7, visible: true })), '')
})

test("a guest's chat card says who asked and that only the owner answers", () => {
  const app = render(createElement(KeyRequestBlock, { block: { id: '7-maps' } }))
  assert.match(app, /An app asked for a key\. Only the owner of this Möbius can answer\./)
  assert.doesNotMatch(app, /Your agent|<button|<input/)
  const agent = render(createElement(KeyRequestBlock, { block: { id: 'agent-maps' } }))
  assert.match(agent, /Your agent asked for a key\. Only the owner of this Möbius can answer\./)
})

test('Settings › Keys, its page and a key page tell a guest only the owner manages keys, instead of showing nothing', () => {
  const summary = render(createElement(KeysSummary, { onOpen() {} }))
  assert.match(summary, /Only the owner of this Möbius can see and manage keys\./)
  assert.doesNotMatch(summary, /maps|<button/)
  const list = render(createElement(KeysPage, { onOpenKey() {}, onAddKey() {} }))
  assert.match(list, /Only the owner of this Möbius can see and manage keys\./)
  assert.doesNotMatch(list, /maps|settings-item__title/)
  const page = render(createElement(KeyPage, { name: 'maps', onOpenKey() {}, onClose() {} }))
  assert.match(page, /Only the owner of this Möbius can see and manage keys\./)
})
