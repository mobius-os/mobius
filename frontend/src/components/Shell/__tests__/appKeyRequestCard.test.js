// An app's pending key request waits over that app's window while the owner
// looks at it, with Later to put it off until the page reloads; the Shell
// re-reads the vault's requests when a notification arrives, so a new
// request shows without polling. Guests: keyRequestSharedBrowserGuest.test.js.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import AppKeyRequestCard, { putOffAppKeyRequests } from '../../AppCanvas/AppKeyRequestCard.jsx'
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

test("a visible app shows its own pending request over its window, with Later", () => {
  const html = render({ appId: 7, visible: true }, waiting)
  assert.match(html, /<strong>Weather<\/strong>/)
  assert.match(html, /<strong>maps<\/strong>/)
  assert.doesNotMatch(html, /notes_api/)
  assert.match(html, />Later<\/button>/)
  assert.match(html, /One more request follows\./)
})

test("the card stays hidden while the app is not visible", () => {
  assert.equal(render({ appId: 7, visible: false }, waiting), '')
})

test('an app with nothing pending shows no card', () => {
  assert.equal(render({ appId: 9, visible: true }, waiting), '')
})

// Later is module state for the page: keep these last.
test('Later puts off exactly the requests shown, until a replacement or another app asks', () => {
  putOffAppKeyRequests(waiting.filter(request => request.app === 7))
  assert.equal(render({ appId: 7, visible: true }, waiting), '')
  // Reopening the app keeps them put off.
  assert.equal(render({ appId: 7, visible: true }, waiting), '')
  assert.match(render({ appId: 8, visible: true }, waiting), /<strong>notes_api<\/strong>/)
  // A replacement under the same id carries a new ticket, so it shows again.
  assert.match(render({ appId: 7, visible: true }, [appAsk(7, 'maps', { ticket: 't-new' })]), /<strong>maps<\/strong>/)
})

// -- the Shell -----------------------------------------------------------------

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

test("a new notification re-reads the vault's requests, and only the list, not an open key page's activity", () => {
  const branch = shell.match(/ev\.type === 'notification_created'\) \{([\s\S]*?)\n {4}\}\n {2}\}, \[/)?.[1] || ''
  assert.match(branch, /onNotificationCreated\(\)/)
  assert.match(branch, /queryClient\.invalidateQueries\(\{ queryKey: KEYS_QUERY, exact: true \}\)/)
})

test("each app window carries its key request card, for the owner only, while the app is visible", () => {
  assert.match(shell, /\{!isSharedBrowserAccess && \(\s*<Suspense fallback=\{null\}>\s*<AppKeyRequestCard appId=\{id\} visible=\{appRuntimeVisible\} \/>/)
})
