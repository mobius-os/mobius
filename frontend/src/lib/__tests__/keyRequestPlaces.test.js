// Key requests where they are asked: a `mobius-key-request` fence in the
// asking chat and a card over the asking app. A block names a request only
// by id (the card reads everything else from the vault), a chat only
// answers the requests its own agent made, the over-app card shows only that
// app's pending asks, and the owner's follow-up message names the key and
// websites, never a value.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const { keyRequestBlockFromToken, chatKeyRequestView, appKeyRequestsToShow, keyAnswerAsk, KEY_REQUEST_FENCE } =
  await vite.ssrLoadModule('/src/lib/keyRequestPlaces.js')
const { keyAnswerWords, requestAppPaused, requestFormKey } = await vite.ssrLoadModule('/src/lib/keysSettingsText.js')
after(() => vite.close())

const fence = text => ({ type: 'code', lang: KEY_REQUEST_FENCE, text })

// -- the fence ---------------------------------------------------------------

test('a fence naming one agent or app request becomes a key request block', () => {
  assert.deepEqual(keyRequestBlockFromToken(fence('{"id":"agent-maps"}')), { id: 'agent-maps' })
  assert.deepEqual(keyRequestBlockFromToken(fence('{"id": "12-weather_api"}\n')), { id: '12-weather_api' })
})

test('anything but a valid request id stays an ordinary code block', () => {
  for (const text of [
    '{"id":"agent-"}', '{"id":"agent-1maps"}', '{"id":"app-maps"}', '{"id":"agent-maps/../x"}',
    `{"id":"agent-${'k'.repeat(65)}"}`, '{"id":42}', '{"id":null}', '{"toString":null}',
    '["agent-maps"]', '"agent-maps"', 'agent-maps', '{"id":"agent-maps"', '',
  ]) {
    assert.equal(keyRequestBlockFromToken(fence(text)), null, text)
  }
})

test('a block carries only the id, so an agent cannot add its own wording to the card', () => {
  assert.equal(keyRequestBlockFromToken(fence('{"id":"agent-maps","reason":"Trust me"}')), null)
})

test('an oversized fence, another language or a non-code token is not a block', () => {
  assert.equal(keyRequestBlockFromToken(fence(`{"id":"agent-maps"}${' '.repeat(300)}`)), null)
  assert.equal(keyRequestBlockFromToken({ type: 'code', lang: 'json', text: '{"id":"agent-maps"}' }), null)
  assert.equal(keyRequestBlockFromToken({ type: 'paragraph', lang: KEY_REQUEST_FENCE, text: '{"id":"agent-maps"}' }), null)
  assert.equal(keyRequestBlockFromToken(null), null)
})

// -- the chat card -----------------------------------------------------------

const TILES = { host: 'tiles.example.com', methods: ['GET'], paths: ['/*'] }
const agentAsk = {
  id: 'agent-maps', requester: 'agent', app: null, key: 'maps', sites: [TILES],
  status: 'pending', ticket: 't1', chat_id: 'chat-1', key_exists: true,
}
const keys = requests => ({ data: { available: true, keys: [], requests } })
const view = overrides => chatKeyRequestView({ id: 'agent-maps', chatId: 'chat-1', owner: true, keys: keys([agentAsk]), ...overrides })

test('the chat whose agent asked shows the live card', () => {
  assert.deepEqual(view({}), { state: 'pending', request: agentAsk })
})

test("another chat's card for the same request is read-only, answered in Settings", () => {
  assert.equal(view({ chatId: 'chat-2' }).state, 'elsewhere')
})

test('a request that names no chat (an app, or stored before chats were kept) fits any chat', () => {
  assert.equal(view({ keys: keys([{ ...agentAsk, chat_id: null }]), chatId: 'chat-2' }).state, 'pending')
})

test('a viewer who is not the owner never gets the live card', () => {
  assert.deepEqual(view({ owner: false }), { state: 'guest', request: null })
})

test("the card's own answer outlives the request leaving the vault", () => {
  const answered = { request: agentAsk, kind: 'approve' }
  assert.deepEqual(view({ keys: keys([]), answered }), { state: 'answered', request: agentAsk, kind: 'approve' })
})

test('a refused request says so; an allowed or withdrawn one is gone', () => {
  assert.equal(view({ keys: keys([{ ...agentAsk, status: 'denied' }]) }).state, 'denied')
  assert.equal(view({ keys: keys([]) }).state, 'gone')
})

test('an earlier copy of the same card points to the newest one, which answers', () => {
  assert.equal(view({ latest: false }).state, 'repeated')
})

test('loading, a stopped vault and a failed read each say so rather than claiming the request is gone', () => {
  assert.equal(view({ keys: {} }).state, 'loading')
  assert.equal(view({ keys: { data: { available: false, keys: [], requests: [] } } }).state, 'unavailable')
  assert.equal(view({ keys: { error: new Error('Keys: 403') } }).state, 'error')
})

// -- the over-app card -------------------------------------------------------

const appAsk = (app, key, extra = {}) => ({
  id: `${app}-${key}`, requester: 'app', app, key, status: 'pending', ticket: `t-${key}`, sites: [TILES], ...extra,
})

test("an app's window shows only that app's pending requests", () => {
  const data = { requests: [appAsk(7, 'maps'), appAsk(8, 'other'), appAsk(7, 'old', { status: 'denied' }), agentAsk] }
  assert.deepEqual(appKeyRequestsToShow(data, 7).map(r => r.id), ['7-maps'])
  // The shell names apps by string id in its tabs.
  assert.deepEqual(appKeyRequestsToShow(data, '7').map(r => r.id), ['7-maps'])
  assert.deepEqual(appKeyRequestsToShow(undefined, 7), [])
})

test('Later puts off that exact request; a replacement under the same id shows again', () => {
  const first = appAsk(7, 'maps')
  const later = new Set([requestFormKey(first)])
  assert.deepEqual(appKeyRequestsToShow({ requests: [first] }, 7, later), [])
  const replaced = { ...first, ticket: 'new-ticket' }
  assert.deepEqual(appKeyRequestsToShow({ requests: [replaced] }, 7, later), [replaced])
})

test('a switched-off app asking for more websites is marked paused on its card', () => {
  const keyList = [{ name: 'maps', grants: { apps: [{ id: 7, sites: [], paused: true }] } }]
  assert.equal(requestAppPaused(keyList, appAsk(7, 'maps')), true)
  assert.equal(requestAppPaused(keyList, appAsk(8, 'maps')), false)
  assert.equal(requestAppPaused(undefined, appAsk(7, 'maps')), false)
})

// -- the owner's answer, in words --------------------------------------------

test("allowing in the chat tells the agent the key and websites, never the value", () => {
  assert.deepEqual(keyAnswerWords(agentAsk, 'approve', {}), {
    message: 'Allowed: your agent may send maps to tiles.example.com.',
    line: 'You allowed your agent to send maps to tiles.example.com.',
  })
  const pasted = keyAnswerWords({ ...agentAsk, key_exists: false, value: 'sk-secret' }, 'approve', {})
  assert.doesNotMatch(JSON.stringify(pasted), /sk-secret/)
})

test('Not now tells the agent it may not send the key there', () => {
  assert.deepEqual(keyAnswerWords(agentAsk, 'deny', {}), {
    message: 'Not now: your agent may not send maps to tiles.example.com.',
    line: 'You said no to your agent sending maps to tiles.example.com.',
  })
})

test('an answer for an ask naming no websites, or from an app, still reads as a sentence', () => {
  assert.equal(keyAnswerWords({ ...agentAsk, sites: [] }, 'approve', {}).message, 'Allowed: your agent may use maps.')
  assert.equal(keyAnswerWords({ ...agentAsk, sites: [] }, 'deny', {}).line, 'You said no to your agent using maps.')
  assert.equal(keyAnswerWords(appAsk(7, 'maps'), 'approve', { 7: 'Weather' }).message,
    'Allowed: Weather may send maps to tiles.example.com.')
})

test('the follow-up message has one identity per request, ticket and answer', () => {
  const same = keyAnswerAsk(agentAsk, 'approve')
  assert.deepEqual(keyAnswerAsk({ ...agentAsk }, 'approve'), same)
  assert.notDeepEqual(keyAnswerAsk(agentAsk, 'deny'), same)
  assert.notDeepEqual(keyAnswerAsk({ ...agentAsk, ticket: 't2' }, 'approve'), same)
})
