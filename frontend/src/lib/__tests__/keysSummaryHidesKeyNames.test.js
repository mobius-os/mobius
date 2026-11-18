// The Keys row on the main Settings page shows only how many keys there are
// and when one was last used: opening Settings must never reveal which
// services the owner holds keys for. Their names appear only on the Keys page.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import KeysSummary from '../../components/SettingsView/KeysSection.jsx'
import { KEYS_QUERY } from '../../components/SettingsView/keysQueries.js'

const SITES = [{ host: 'api.nasa.gov', methods: ['GET'], paths: ['/*'] }]

function overviewRow(keys, requests = []) {
  const client = new QueryClient()
  client.setQueryData(KEYS_QUERY, { available: true, keys, requests })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(KeysSummary, { onOpen() {} })))
}

test("the overview row counts the owner's keys without naming them", () => {
  const row = overviewRow([
    { name: 'nasa', grants: { sites: SITES }, last_used: new Date(Date.now() - 60_000).toISOString() },
    { name: 'tiles', grants: {} },
  ], [{
    id: 'agent-maps', requester: 'agent', key: 'maps', status: 'pending', ticket: 't1',
    sites: SITES, chat_id: null, key_exists: false, granted: false,
  }])
  assert.match(row, /2 keys/)
  assert.match(row, /Last used/)
  assert.match(row, /1 needs you/)
  assert.doesNotMatch(row, /nasa|tiles|maps/)
})

test('one key reads as one key, still unnamed', () => {
  const row = overviewRow([{ name: 'nasa', grants: {} }])
  assert.match(row, /1 key</)
  assert.match(row, /Not used yet/)
  assert.doesNotMatch(row, /nasa/)
})
