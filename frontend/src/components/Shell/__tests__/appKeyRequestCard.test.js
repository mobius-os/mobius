// An app's pending key request is asked in the moment: a dialog in the
// middle of that app's window while the owner looks at it, with Not now as
// the only way out. A new request never notifies; the server's
// key_requests_changed nudge makes the Shell re-read the vault's requests, so
// it shows without polling. It is drawn only while the owner can see it, so a
// card revealed later is a new one whose answers wait again. Guests:
// keyRequestSharedBrowserGuest.test.js.
import assert from 'node:assert/strict'
import { afterEach, test } from 'node:test'
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

afterEach(() => { delete globalThis.document })

test("a visible app asks its own pending request in a dialog, with no way to put it off", () => {
  const html = render({ appId: 7 }, waiting)
  assert.match(html, /role="dialog" aria-modal="true" aria-label="Weather asks to use maps"/)
  assert.match(html, /<span>Let <\/span><strong>Weather<\/strong>/)
  assert.match(html, /<strong>maps<\/strong>/)
  assert.match(html, /Weather says:<\/span> Fetch tiles/)
  assert.doesNotMatch(html, /notes_api|Later/)
  assert.match(html, />Not now<\/button>/)
  assert.match(html, /One more request follows\./)
})

test('the dialog says what to weigh and that Not now is a no that stays', () => {
  const html = render({ appId: 7 }, [appAsk(7, 'maps')])
  assert.match(html, /Weather wants your maps key sent to tiles\.example\.com\. Allow only if you trust that website with it\./)
  assert.match(html, /Not now is a no: Weather can(&#x27;|')t ask for maps again until you let it in Settings › Keys\./)
  assert.doesNotMatch(html, /never sees the key/)
})

test('Not now and Allow start asleep, so a dialog appearing under the pointer takes no click', () => {
  const html = render({ appId: 7 }, [appAsk(7, 'maps')])
  assert.match(html, /aria-disabled="true"[^>]*>Not now<\/button>/)
  assert.match(html, /aria-disabled="true"[^>]*>Allow<\/button>/)
})

test("a key the owner doesn't have yet is asked first; the paste field comes after Continue", () => {
  const html = render({ appId: 7 }, [appAsk(7, 'maps', { key_exists: false })])
  assert.match(html, /<span>Let <\/span><strong>Weather<\/strong><span> use a key<\/span><span> on <\/span><strong>tiles\.example\.com<\/strong>/)
  assert.match(html, />Continue<\/button>/)
  assert.doesNotMatch(html, /type="password"|>Allow</)
})

test("another app's key keeps Allow behind \"Use <app>'s key anyway\"", () => {
  const html = render({ appId: 7 }, [appAsk(7, 'maps', { key_origin: { app: 8, name: 'maps' } })])
  assert.match(html, /<summary>Use Notes(&#x27;|')s key anyway<\/summary>/)
  assert.doesNotMatch(html, />Allow<\/button>/)
})

test('the card is not drawn while something else is over the app, so it waits afresh once uncovered', () => {
  assert.equal(render({ appId: 7, covered: true }, waiting), '')
})

test('the card is not drawn while the page is hidden, so it waits afresh when the owner comes back', () => {
  globalThis.document = { visibilityState: 'hidden' }
  assert.equal(render({ appId: 7 }, waiting), '')
  globalThis.document = { visibilityState: 'visible' }
  assert.match(render({ appId: 7 }, waiting), /role="dialog"/)
})

test('an app with nothing pending shows no card', () => {
  assert.equal(render({ appId: 9 }, waiting), '')
})
