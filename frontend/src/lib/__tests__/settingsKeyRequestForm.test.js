// Settings › Keys: each pending app request gets its own answer form, keys an
// app saved name that app, and the plain-language decisions behind each screen
// (request card wording, paste-only Add naming, "sent only to", Used by).
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
  requestWording, requestAppName, serviceForValue, freeKeyName, newKeyName, hostList, keyHosts, sentToLine,
  usedByLine, usedByRows, lastUseByCaller, usageWhat, approvalBody, BEARER,
} = await vite.ssrLoadModule('/src/lib/keysSettingsText.js')
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

// -- request cards in plain words --------------------------------------------

const leadText = words => words.lead.map(part => (typeof part === 'string' ? part : part.text)).join('')
const site = host => ({ host, paths: ['/data/2.5/*'], methods: ['GET'] })
const weatherAsk = { ...ask, app: 8, key: 'openweather', sites: [site('api.openweathermap.org')], reason: 'Shows the forecast.' }
const appNames = { 8: 'Weather', 10: 'Modelapp' }

test("an app asking for the owner's existing key asks whether to let it, naming the key and where it goes", () => {
  const words = requestWording({ ...weatherAsk, key_exists: true, key_origin: null }, appNames)
  assert.equal(leadText(words), 'Let Weather use your openweather key on api.openweathermap.org?')
  assert.equal(words.says, 'Weather says:')
  // The website the app names may be its author's own: the question is whether to trust it with the key.
  assert.equal(words.note, 'Weather wants your openweather key sent to api.openweathermap.org. Allow only if you trust that website with it.')
  assert.equal(words.denyNote, "Not now is a no: Weather can't ask for openweather again until you let it in Settings › Keys.")
  assert.equal(words.needsValue, false)
  assert.equal(words.allowHidden, false)
})

test('an app asking for a key that does not exist yet asks first, then for the value by its website', () => {
  const words = requestWording({ ...weatherAsk, key_exists: false }, appNames)
  // The app's own name for the key is a detail; the question is about the website.
  assert.equal(leadText(words), 'Let Weather use a key on api.openweathermap.org?')
  assert.equal(words.needsValue, true)
  assert.equal(words.pasteTitle, 'Paste your api.openweathermap.org key')
  assert.equal(words.pasteNote, "Saved as “openweather”. You'll find it in Settings › Keys.")
  assert.equal(requestWording({ ...weatherAsk, key_exists: false, sites: [] }, appNames).pasteTitle, 'Paste the key')
})

test('an app asking for more websites lists the new ones', () => {
  const words = requestWording({
    ...weatherAsk, key_exists: true, granted: true, sites: [site('a.example.com'), site('b.example.com')],
  }, appNames)
  assert.equal(leadText(words), 'Let Weather use openweather on more websites: a.example.com and b.example.com?')
  assert.equal(words.note, 'Weather wants openweather sent to a.example.com and b.example.com too. Allow only if you trust those websites with it.')
})

test("the owner's agent asking reads as the agent sending the key to a website", () => {
  const agentAsk = {
    id: 'agent-github', requester: 'agent', app: null, app_name: 'Your agent', key: 'github',
    key_exists: true, sites: [site('api.github.com')], ticket: 't', status: 'pending',
  }
  assert.equal(requestAppName(agentAsk, appNames), 'Your agent')
  const words = requestWording(agentAsk, appNames)
  assert.equal(words.kind, 'agent')
  assert.equal(leadText(words), 'Let your agent send github to api.github.com?')
  assert.equal(words.says, 'Your agent says:')
  assert.equal(words.needsValue, false)
  assert.equal(requestWording({ ...agentAsk, key_exists: false }, appNames).needsValue, true)
})

test("an agent's ask never borrows an app's name, even one that shares its id slot", () => {
  const words = requestWording({ requester: 'agent', app: null, key: 'k', key_exists: true, sites: [] }, { null: 'Weather' })
  assert.equal(words.who, 'Your agent')
})

test("another app's key keeps Allow behind an explicit 'anyway' and warns whose key it is", () => {
  const words = requestWording({ ...weatherAsk, key_exists: true, key_origin: { app: 10, name: 'openweather' } }, appNames)
  assert.equal(words.allowHidden, true)
  assert.equal(words.savedBy, 'Modelapp')
  assert.match(words.warning, /^This key belongs to Modelapp, not you/)
  assert.match(words.warning, /Weather's requests with Modelapp's key\.$/)
  // Nothing the owner could not do: an app asks for the key it asks for.
  assert.doesNotMatch(words.warning, /add a key of yours/)
  assert.equal(words.anyway.label, "Use Modelapp's key anyway")
  assert.match(words.anyway.note, /signed with Modelapp's account/)
})

test("more websites on another app's key is still kept behind 'anyway'", () => {
  const words = requestWording({ ...weatherAsk, key_exists: true, granted: true, key_origin: { app: 10, name: 'x' } }, appNames)
  assert.equal(words.kind, 'more-sites')
  assert.equal(words.allowHidden, true)
})

test('an ask naming no websites says the app cannot send it anywhere yet', () => {
  const words = requestWording({ ...weatherAsk, key_exists: true, sites: [] }, appNames)
  assert.match(words.note, /^It names no websites, so Weather can't send this key anywhere/)
})

// -- adding a key: paste, optional name ---------------------------------------

test('a pasted key with a known prefix is named after its service', () => {
  assert.equal(serviceForValue('ghp_abc'), 'GitHub')
  assert.equal(serviceForValue('github_pat_abc'), 'GitHub')
  assert.equal(serviceForValue('  sk-ant-api03-x'), 'Anthropic')
  assert.equal(serviceForValue('sk-proj-x'), 'OpenAI')
  assert.equal(serviceForValue('sk-x'), 'OpenAI')
  assert.equal(serviceForValue('AIzaSy'), 'Google')
  assert.equal(serviceForValue('xoxb-1'), 'Slack')
  assert.equal(serviceForValue('xoxp-1'), 'Slack')
  assert.equal(serviceForValue('AKIAIOSFODNN7'), 'AWS')
  assert.equal(serviceForValue('glpat-x'), 'GitLab')
  assert.equal(serviceForValue('d41d8cd98f00b204'), null)
})

test('an unrecognised key with no typed name gets a generic name that never takes an existing one', () => {
  assert.equal(newKeyName('', 'd41d8cd98f', []).name, 'key')
  assert.equal(newKeyName('', 'd41d8cd98f', ['key']).name, 'key-2')
  assert.equal(newKeyName('', 'd41d8cd98f', ['key', 'Key-2']).name, 'key-3')
})

test('a service name already in use gets a number instead of replacing that key', () => {
  assert.equal(freeKeyName('GitHub', ['github']), 'GitHub-2')
  assert.deepEqual(newKeyName('', 'ghp_x', ['GitHub']), { name: 'GitHub-2', service: 'GitHub', valid: true, taken: false })
})

test("the owner's typed name wins, and an existing or malformed one is flagged", () => {
  assert.deepEqual(newKeyName(' work-github ', 'ghp_x', []), { name: 'work-github', service: null, valid: true, taken: false })
  assert.equal(newKeyName('openweather', 'x', ['OpenWeather']).taken, true)
  assert.equal(newKeyName('my token', 'x', []).valid, false)
})

// -- where a key goes, and who uses it ----------------------------------------

const keyWith = grants => ({ name: 'openweather', grants: { apps: [], agents: false, sites: [], ...grants } })
const appGrant = (id, hosts, paused = false) => ({ id, sites: hosts.map(site), paused })

test('a key with no websites says it is sent nowhere yet', () => {
  assert.equal(sentToLine(keyWith({ agents: true })), 'Not sent anywhere yet')
})

test("'sent only to' covers the owner's sites and each app's, without repeats", () => {
  const key = keyWith({ sites: [site('api.github.com')], apps: [appGrant(8, ['api.github.com', 'uploads.github.com'])] })
  assert.deepEqual(keyHosts(key), ['api.github.com', 'uploads.github.com'])
  assert.equal(sentToLine(key), 'Sent only to api.github.com and uploads.github.com')
})

test("a switched-off app's websites are not where the key is sent", () => {
  const key = keyWith({ apps: [appGrant(8, ['api.openweathermap.org'], true)] })
  assert.equal(sentToLine(key), 'Not sent anywhere yet')
})

test('many hosts are summarised instead of listed in full', () => {
  assert.equal(hostList(['a', 'b', 'c']), 'a, b and c')
  assert.equal(hostList(['a', 'b', 'c', 'd', 'e']), 'a, b and 3 more')
})

test('the overview names the apps and agents using a key, or says nothing uses it', () => {
  assert.equal(usedByLine(keyWith({ apps: [appGrant(8, []), appGrant(9, [])], agents: true }), { 8: 'Weather', 9: 'Notes' }),
    'Used by Weather, Notes · your agents')
  assert.equal(usedByLine(keyWith({ agents: true }), {}), 'Used by your agents')
  assert.equal(usedByLine(keyWith({}), {}), 'Not used yet')
  assert.equal(usedByLine(keyWith({ apps: [appGrant(8, [], true)] }), appNames), 'Switched off for Weather')
})

test("each app's Used by switch is on unless the app is paused, and the agents row follows grants.agents", () => {
  const now = Date.parse('2026-11-16T12:00:00Z')
  const rows = usedByRows(
    keyWith({ apps: [appGrant(8, ['a.example.com']), appGrant(10, ['a.example.com'], true)], agents: true }),
    appNames,
    { 'app:8': '2026-11-16T11:58:00Z' },
    now,
  )
  assert.deepEqual(rows.apps.map(row => [row.label, row.initial, row.on, row.sub]), [
    ['Weather', 'W', true, 'Last used 2 min ago'],
    ['Modelapp', 'M', false, 'Switched off'],
  ])
  assert.equal(rows.agents.on, true)
  assert.equal(rows.agents.sub, 'Can use it and read it')
  assert.equal(usedByRows(keyWith({}), {}).agents.on, false)
})

test('an app that was never used says whether it has websites yet', () => {
  const rows = usedByRows(keyWith({ apps: [appGrant(8, []), { id: 9, sites: [site('x.example.com')] }] }), appNames)
  assert.deepEqual(rows.apps.map(row => row.sub), ['No websites yet', 'Can use it'])
  assert.equal(rows.apps[1].on, true)
})

test('last use per user comes from the newest use, with all agents together', () => {
  // The vault logs an app by its id and installation.
  const install = '0123456789abcdef0123456789abcdef'
  const last = lastUseByCaller([
    { caller: `app:8:${install}`, outcome: 'used', ts: '3' },
    { caller: 'agent:chat-a', outcome: 'revealed', ts: '2' },
    { caller: `app:8:${install}`, outcome: 'used', ts: '1' },
    { caller: `app:9:${install}`, outcome: 'denied', ts: '1' },
  ])
  assert.deepEqual(last, { 'app:8': '3', agent: '2' })
  assert.equal(usageWhat({ caller: `app:8:${install}`, outcome: 'used', host: 'api.x', ts: '3' }, appNames),
    'Weather used it')
})

test('recent activity reads as sentences, including refusals of a switched-off app', () => {
  const at = '2026-11-16T11:58:00Z'
  assert.equal(usageWhat({ caller: 'app:8', outcome: 'used', host: 'api.x', ts: at }, appNames), 'Weather used it')
  assert.equal(usageWhat({ caller: 'agent:c1', outcome: 'revealed', host: '-', ts: at }, appNames), 'Your agent read it')
  assert.equal(usageWhat({ caller: 'app:8', outcome: 'denied', host: 'api.x', reason: 'paused', ts: at }, appNames),
    'Weather was refused (switched off)')
  assert.equal(usageWhat({ caller: 'owner', outcome: 'granted app:8', host: '-', ts: at }, appNames), 'You let Weather use it')
  assert.equal(usageWhat({ caller: 'owner', outcome: 'something new', host: '-', ts: at }, appNames), null)
  // The vault writes an agent request's answers as "<verb> agents".
  assert.equal(usageWhat({ caller: 'owner', outcome: 'granted agents', host: '-', ts: at }, appNames), 'You let your agents use it')
  assert.equal(usageWhat({ caller: 'owner', outcome: 'denied agents', host: '-', ts: at }, appNames), 'You said no to your agent')
  assert.equal(usageWhat({ caller: 'agent:c1', outcome: 'requested', host: '-', ts: at }, appNames), 'Your agent asked to use it')
})

// Review follow-ups: the safe default covers agents, and the cards never
// promise more (or less) than the vault does.
test("an agent asking for a key an app saved sees that it is the app's key, behind an explicit anyway", () => {
  const words = requestWording({
    id: 'agent-app7-notion', requester: 'agent', app: null, key: 'app7-notion', key_exists: true,
    key_origin: { app: 7, name: 'notion' }, sites: [{ host: 'api.notion.com', methods: ['GET'], paths: ['/*'] }],
  }, { 7: 'Notes' })
  assert.equal(words.kind, 'agent')
  assert.equal(words.allowHidden, true)
  assert.match(words.warning, /belongs to Notes, not you/)
  assert.match(words.warning, /lets your agents read Notes's key and send it/)
  // An agent can ask for another key, so the owner is told how to get one.
  assert.match(words.warning, /choose Not now and tell your agent which one\.$/)
  assert.equal(words.denyNote, "Not now is a no: your agent can't ask for app7-notion again until you let it in Settings › Keys.")
})

test("an agent's ask names where the key would go, and claims no site when it names none", () => {
  const base = { id: 'agent-k', requester: 'agent', app: null, key: 'k', key_exists: true, key_origin: null }
  assert.equal(requestWording({ ...base, sites: [] }, {}).note, 'Allowing lets your agents read and use it.')
  assert.equal(requestWording({ ...base, sites: [{ host: 'api.x.com', methods: ['GET'], paths: ['/*'] }] }, {}).note,
    'Allowing lets your agents read it and send it to api.x.com.')
})

test('a switched-off app asking for more websites is told it stays off', () => {
  const request = { id: '8-k', requester: 'app', app: 8, key: 'k', key_exists: true, granted: true,
                    sites: [{ host: 'api.x.com', methods: ['GET'], paths: ['/*'] }] }
  assert.doesNotMatch(requestWording(request, { 8: 'Weather' }).note, /switched off/)
  assert.match(requestWording({ ...request, app_paused: true }, { 8: 'Weather' }).note,
    /Weather is switched off for this key and stays off until you switch it back on/)
})

// -- how a key is sent ---------------------------------------------------------
// Settings saves a pasted key as a bearer token. The first request allowed for
// a key nothing sends yet may set another slot; a key in use keeps its own.

const querySlot = { type: 'query', name: 'appid' }
const slotAsk = { ...weatherAsk, ticket: 't1', inject: querySlot, key_origin: null }

// How a key is sent is a detail under Details, not part of the question.
test('a card that sets how a key is sent says so under Details, and Allow sends exactly that slot', () => {
  const card = { ...slotAsk, key_exists: true, sets_inject: true, inject_kept: false }
  const words = requestWording(card, appNames)
  assert.deepEqual(words.slot, querySlot)
  assert.equal(words.keptSlot, null)
  assert.doesNotMatch(words.note, /appid|sent as/i)
  assert.deepEqual(approvalBody(card, ''), { ticket: 't1', sites: card.sites, inject: querySlot })
})

test('a key something already sends keeps its slot, and its card says which', () => {
  const card = { ...slotAsk, key_exists: true, sets_inject: false, inject_kept: true, key_inject: BEARER }
  const words = requestWording(card, appNames)
  assert.equal(words.slot, null)
  assert.deepEqual(words.keptSlot, { kept: BEARER, asked: querySlot })
  assert.doesNotMatch(words.note, /Bearer|appid/)
  assert.deepEqual(approvalBody(card, ''), { ticket: 't1', sites: card.sites })
})

test('a new key is saved with its value and the slot its request named, or a bearer token', () => {
  assert.deepEqual(approvalBody({ ...slotAsk, key_exists: false }, 'v'),
    { ticket: 't1', sites: slotAsk.sites, value: 'v', inject: querySlot })
  assert.deepEqual(approvalBody({ ...slotAsk, key_exists: false, inject: null }, 'v').inject, BEARER)
  assert.equal(requestWording({ ...slotAsk, key_exists: false }, appNames).note,
    'Weather wants a key sent to api.openweathermap.org. Allow only if you trust that website with it.')
})
