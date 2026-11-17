// An app's pending key request is asked in the moment: a dialog in the
// middle of that app's window while the owner looks at it, with Not now as
// the only way out. A new request never notifies; the server's
// key_requests_changed nudge makes the Shell re-read the vault's requests, so
// it shows without polling. Guests: keyRequestSharedBrowserGuest.test.js.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppKeyRequestCard from '../../AppCanvas/AppKeyRequestCard.jsx'
import { KEYS_QUERY } from '../../SettingsView/keysQueries.js'
import { appQueries } from '../../../hooks/queries.js'

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const appAsk = (app, key, extra = {}) => ({
  id: `${app}-${key}`, requester: 'app', app, app_name: `App ${app}`, key, reason: 'Fetch tiles', status: 'pending',
  ticket: `t-${key}`, sites: [TILES], inject: null, chat_id: null, key_exists: true, granted: false, ...extra,
})

function render(props, requests) {
  const client = new QueryClient()
  client.setQueryData(appQueries.list.key, [{ id: 7, name: 'Weather' }, { id: 8, name: 'Notes' }])
  client.setQueryData(KEYS_QUERY, { available: true, keys: [], requests })
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(AppKeyRequestCard, props)))
}

const waiting = [appAsk(7, 'maps'), appAsk(7, 'tiles'), appAsk(8, 'notes_api')]

test("a visible app asks its own pending request in a dialog, with no way to put it off", () => {
  const html = render({ appId: 7, visible: true }, waiting)
  assert.match(html, /role="dialog" aria-modal="true" aria-label="Key request"/)
  assert.match(html, /<span>Let <\/span><strong>Weather<\/strong>/)
  assert.match(html, /<strong>maps<\/strong>/)
  assert.match(html, /Weather says:<\/span> Fetch tiles/)
  assert.doesNotMatch(html, /notes_api|Later/)
  assert.match(html, />Not now<\/button>/)
  assert.match(html, /One more request follows\./)
})

test("a key the owner doesn't have yet is asked first; the paste field comes after Continue", () => {
  const html = render({ appId: 7, visible: true }, [appAsk(7, 'maps', { key_exists: false })])
  assert.match(html, /<span>Let <\/span><strong>Weather<\/strong><span> use a key<\/span><span> on <\/span><strong>tiles\.example\.com<\/strong>/)
  assert.match(html, />Continue<\/button>/)
  assert.doesNotMatch(html, /type="password"|>Allow</)
})

test("the card stays hidden while the app is not visible", () => {
  assert.equal(render({ appId: 7, visible: false }, waiting), '')
})

test('an app with nothing pending shows no card', () => {
  assert.equal(render({ appId: 9, visible: true }, waiting), '')
})
