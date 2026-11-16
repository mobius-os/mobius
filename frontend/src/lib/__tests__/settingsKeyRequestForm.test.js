// Settings › Keys: each pending app request gets its own answer form, and keys
// an app saved name that app.
//
// A request id is the app id and key name, so a request that replaces another
// (an app that reused a removed app's id) arrives under the same id with a new
// ticket. Its form must start empty: otherwise React keeps the value typed for
// the old request and Allow sends it with the replacement's ticket.
//
// The frontend unit suite deliberately has no DOM renderer (no jsdom), so this
// checks the form identity KeysSection keys each request card by. The
// behaviour itself was verified with the real KeysSection and KeyRequest in a
// local JSDOM harness: before the fix the old draft stayed in the field and
// Allow sent it with the new ticket; after it the field is empty, Allow is
// disabled, and nothing is sent.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const {
  requestFormKey, savedByApp, replaceWarning, requestSubject, siteLine, shownRequests, distinctAppNames,
} = await vite.ssrLoadModule('/src/components/SettingsView/KeysSection.jsx')
after(() => vite.close())

const request = ticket => ({ id: '5-weather_api', app: 5, key: 'weather_api', status: 'pending', ticket })

test('a request that replaces another under the same id gets a new form', () => {
  assert.notEqual(requestFormKey(request('first-ticket')), requestFormKey(request('second-ticket')))
})

test('the same request keeps its form across refreshes', () => {
  assert.equal(requestFormKey(request('first-ticket')), requestFormKey({ ...request('first-ticket') }))
})

test('requests for different keys never share a form', () => {
  assert.notEqual(
    requestFormKey({ ...request('t'), id: '5-weather_api' }),
    requestFormKey({ ...request('t'), id: '5-maps_api' }),
  )
})

// An app's own saved secret loses that app's grant when anyone else replaces
// its value (keyvault put_key), so Settings names the app before a Replace.
const names = { 7: 'Weather' }
const appSaved = { name: 'api_key', origin: { app: 7, name: 'api_key' }, grants: { apps: [7] } }
const ownerKey = { name: 'api_key', grants: { apps: [7] } }

test('a key an app saved names that app', () => {
  assert.equal(savedByApp(appSaved, names), 'Weather')
  assert.equal(savedByApp({ ...appSaved, origin: { app: 9, name: 'x' } }, names), 'App 9')
})

test("the owner's own key names no app", () => {
  assert.equal(savedByApp(ownerKey, names), null)
  assert.equal(replaceWarning(ownerKey, names), null)
})

test('replacing an app-saved key warns that the app loses access', () => {
  const warning = replaceWarning(appSaved, names)
  assert.match(warning, /^Weather saved this key\./)
  assert.match(warning, /Weather stops using it/)
})

// A key another app saved for itself is that app's credential: the card must
// never call it the owner's, or Allow would quietly sign one app's requests
// with another app's account.
const ask = { id: '8-openweather', app: 8, key: 'openweather', status: 'pending', ticket: 't' }

test("a key another app saved is named as that app's, not the owner's", () => {
  const subject = requestSubject({ ...ask, key_exists: true, key_origin: { app: 10, name: 'openweather' } }, { 10: 'Modelapp' })
  assert.deepEqual(subject, { kind: 'saved-by-other', savedBy: 'Modelapp' })
})

test("an existing key with no app origin is the owner's", () => {
  assert.deepEqual(requestSubject({ ...ask, key_exists: true, key_origin: null }, {}), { kind: 'yours' })
})

test("an ask for more sites on a key another app saved still names that app, not the owner", () => {
  const subject = requestSubject(
    { ...ask, key_exists: true, granted: true, key_origin: { app: 10, name: 'openweather' } }, { 10: 'Modelapp' },
  )
  assert.deepEqual(subject, { kind: 'more-sites', savedBy: 'Modelapp' })
})

test('a new key and an ask for more sites keep their own wording', () => {
  assert.deepEqual(requestSubject({ ...ask, key_exists: false }, {}), { kind: 'new' })
  assert.deepEqual(requestSubject({ ...ask, key_exists: true, granted: true }, {}), { kind: 'more-sites' })
})

test('a site open on every path says so instead of hiding the wildcard', () => {
  assert.equal(siteLine({ host: 'api.example.com', paths: ['/*'], methods: ['GET'] }), 'api.example.com (all paths) · GET')
  assert.equal(siteLine({ host: 'api.example.com', paths: ['/v1/*'], methods: ['GET', 'POST'] }), 'api.example.com /v1/* · GET, POST')
})

test('a burst of requests shows three until the owner asks for the rest', () => {
  const pending = Array.from({ length: 10 }, (_, i) => ({ ...ask, id: `8-k${i}` }))
  assert.equal(shownRequests(pending, false).length, 3)
  assert.equal(shownRequests(pending, true).length, 10)
})

test('apps sharing a name are told apart by their number', () => {
  const apps = distinctAppNames([{ id: 3, name: 'Notes' }, { id: 4, name: 'Notes' }, { id: 5, name: 'Weather' }])
  assert.deepEqual(apps.map(app => app.name), ['Notes (#3)', 'Notes (#4)', 'Weather'])
})
