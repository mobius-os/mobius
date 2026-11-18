// Settings › Keys mostly narrows a key. How it is sent, which apps may use it,
// and where the owner's agents may send it come from the agent or app that asks
// for it, approved on that ask's card. The one widening left here is a website
// for an app that already has the key: an app that never asks (such as one
// whose saved keys moved into the vault without sites) has no other way in.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { KeyPage, SitesRows } from '../../components/SettingsView/KeysSection.jsx'
import { KEYS_QUERY } from '../../components/SettingsView/keysQueries.js'

const SITE = { host: 'api.example.com', methods: ['GET'], paths: ['/*'] }
const KEY = {
  name: 'weather',
  revision: 1,
  inject: { type: 'query', name: 'api_key' },
  grants: { agents: true, sites: [SITE], apps: [] },
}

function keyPage(name) {
  const client = new QueryClient()
  client.setQueryData(KEYS_QUERY, { available: true, keys: [KEY], requests: [] })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(KeyPage, { name, onOpenKey() {}, onClose() {} })))
}

test("a key's page lists its agents' websites but offers no way to add one, let an app in, or change how it is sent", () => {
  const page = keyPage('weather')
  assert.match(page, /Remove api\.example\.com/)
  assert.match(page, /Used by/)
  assert.doesNotMatch(page, /Add a website/)
  assert.doesNotMatch(page, /Let an app use/)
  assert.doesNotMatch(page, /How it(&#x27;|')s sent/)
  assert.doesNotMatch(page, /Header name|Parameter name|In the address/)
})

test("an app's own website list still lets the owner add one", () => {
  const list = props => renderToStaticMarkup(createElement(SitesRows, { sites: [SITE], onChange() {}, busy: false, empty: 'None', ...props }))
  assert.match(list({ canAdd: true }), /Add a website/)
  assert.doesNotMatch(list({}), /Add a website/)
})

test('adding a key asks only for the key, leaving where it goes to the first ask', () => {
  const form = keyPage('')
  assert.match(form, /Save key/)
  assert.match(form, /sent nowhere yet/)
  assert.doesNotMatch(form, /already know which website/)
  assert.doesNotMatch(form, /Header name|Parameter name|In the address/)
})
