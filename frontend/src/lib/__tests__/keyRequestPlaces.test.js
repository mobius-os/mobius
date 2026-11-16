// Key requests are asked in the moment: a `mobius-key-request` fence in the
// asking chat and a dialog over the asking app, with the app list marking an
// app whose request waits and Settings › Keys answering only what has no
// other place. A block names a request only
// by id (the card reads everything else from the vault), a chat only
// answers the requests its own agent made, the over-app card shows only that
// app's pending asks, and the owner's follow-up message names the key and
// websites, never a value.
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createSsrRenderServer } from './ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
const {
  keyRequestBlockFromToken, chatKeyRequestView, appKeyRequestsToShow, appsWaitingForKeys, settingsKeyRequests, keyAnswerAsk, goneKeyRequestLine, tellAgentKeyAnswer,
  KEY_REQUEST_FENCE,
} = await vite.ssrLoadModule('/src/lib/keyRequestPlaces.js')
const { createAppBlockChat, askCid } = await vite.ssrLoadModule('/src/components/ChatView/markdown/appBlockChat.js')
const { keyAnswerWords, requestAppPaused } = await vite.ssrLoadModule('/src/lib/keysSettingsText.js')
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
  assert.deepEqual(view({ owner: false }), { state: 'guest', request: null, asker: 'agent' })
})

test("a guest's card for an app's request says an app asked, not their agent", () => {
  assert.deepEqual(view({ id: '7-maps', owner: false }), { state: 'guest', request: null, asker: 'app' })
})

test("an app's request in a chat is read-only: a chat answers only its own agent", () => {
  const appRequest = { ...agentAsk, id: '7-maps', requester: 'app', app: 7, chat_id: null }
  for (const chatId of ['chat-1', 'chat-2', null]) {
    assert.deepEqual(view({ id: '7-maps', chatId, keys: keys([appRequest]) }), { state: 'app', request: appRequest })
  }
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

test('the app list marks every app with a pending request, and no agent or answered one', () => {
  const data = { requests: [appAsk(7, 'maps'), appAsk(8, 'old', { status: 'denied' }), appAsk(9, 'x'), agentAsk] }
  assert.deepEqual([...appsWaitingForKeys(data)].sort(), ['7', '9'])
  assert.deepEqual([...appsWaitingForKeys(undefined)], [])
})

test("Settings › Keys answers only an agent's request that names no chat; every other ask is answered where it was made", () => {
  const chatless = { ...agentAsk, id: 'agent-old', chat_id: null }
  const data = { requests: [appAsk(7, 'maps'), { ...agentAsk, chat_id: 'chat-1' }, chatless, { ...chatless, id: 'agent-no', status: 'denied' }] }
  assert.deepEqual(settingsKeyRequests(data).map(r => r.id), ['agent-old'])
  assert.deepEqual(settingsKeyRequests(undefined), [])
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

// -- telling the asking agent ------------------------------------------------

const FINGERPRINT = JSON.stringify([KEY_REQUEST_FENCE, 'agent-maps'])

function chatSending(result) {
  const sent = []
  const chat = createAppBlockChat('chat-1', {
    send: async (message, options) => {
      sent.push({ message, ...options })
      if (result instanceof Error) throw result
      return result
    },
  })
  return { sent, ask: request => chat.ask('mobius-key-request\nagent-maps', {}, request) }
}

test("answering in the chat posts one owner message naming the key, its websites and the answer", async () => {
  const { sent, ask } = chatSending(true)
  assert.equal(await tellAgentKeyAnswer(ask, agentAsk, 'approve', {}), true)
  const cid = await askCid('chat-1', FINGERPRINT, [{ id: 'agent-maps', code: 'approve', rev: 't1' }])
  assert.deepEqual(sent, [{ message: 'Allowed: your agent may send maps to tiles.example.com.', cid }])
  await tellAgentKeyAnswer(ask, agentAsk, 'deny', {})
  assert.equal(sent[1].message, 'Not now: your agent may not send maps to tiles.example.com.')
  assert.notEqual(sent[1].cid, cid)
})

test("the follow-up's message id comes from the request, its ticket and the answer, so a retry posts it once", async () => {
  const { sent, ask } = chatSending(true)
  await tellAgentKeyAnswer(ask, agentAsk, 'approve', {})
  await tellAgentKeyAnswer(ask, { ...agentAsk }, 'approve', {})
  await tellAgentKeyAnswer(ask, { ...agentAsk, ticket: 't2' }, 'approve', {})
  assert.equal(sent[0].cid, sent[1].cid)
  assert.notEqual(sent[2].cid, sent[0].cid)
})

test("a follow-up the chat could not send reports failure, so the card can say so", async () => {
  assert.equal(await tellAgentKeyAnswer(chatSending(false).ask, agentAsk, 'approve', {}), false)
  assert.equal(await tellAgentKeyAnswer(chatSending(new Error('offline')).ask, agentAsk, 'approve', {}), false)
  // The server already had this id: the agent was told.
  assert.equal(await tellAgentKeyAnswer(chatSending('duplicate').ask, agentAsk, 'approve', {}), true)
})

// -- after a reload ----------------------------------------------------------

const mapsKey = { name: 'maps', grants: { agents: true, sites: [TILES, { ...TILES, host: 'api.example.com' }], apps: [] } }
const at = (outcome, ts, { key = 'maps', caller = 'owner' } = {}) => ({ key, caller, outcome, ts })
const asked = (chat, ts, key = 'maps') => at('requested', ts, { key, caller: `agent:${chat}` })
const gone = (usage, chatId = 'chat-1', keyList = [mapsKey]) => goneKeyRequestLine('agent-maps', chatId, keyList, usage)

test("an allowed request that left the vault still says what the owner allowed, from the key's activity", () => {
  const usage = [
    at('used', '2026-11-16T10:05:00Z', { caller: 'agent:chat-1' }),
    at('granted agents', '2026-11-16T10:00:00Z'),
    asked('chat-1', '2026-11-16T09:59:00Z'),
  ]
  assert.equal(gone(usage), 'You allowed your agent to send maps to tiles.example.com and api.example.com.')
})

test('the key update an approval writes in the same second does not hide its answer, in either order', () => {
  for (const usage of [
    [at('updated', '2026-11-16T10:00:00Z'), at('granted agents', '2026-11-16T10:00:00Z'), asked('chat-1', '2026-11-16T09:59:00Z')],
    [at('granted agents', '2026-11-16T10:00:00Z'), at('updated', '2026-11-16T10:00:00Z'), asked('chat-1', '2026-11-16T09:59:00Z')],
  ]) {
    assert.match(gone(usage), /^You allowed your agent/)
  }
})

test("a refusal of this chat's ask names no websites", () => {
  assert.equal(gone([at('denied agents', '2026-11-16T10:00:00Z'), asked('chat-1', '2026-11-16T09:59:00Z')]),
    'You said no to your agent using maps.')
})

test("another chat's later refusal is not this chat's answer", () => {
  const usage = [
    at('denied agents', '2026-11-16T12:00:00Z'),
    asked('chat-2', '2026-11-16T11:59:00Z'),
    at('granted agents', '2026-11-16T10:00:00Z'),
    at('updated', '2026-11-16T10:00:00Z'),
    asked('chat-1', '2026-11-16T09:59:00Z'),
  ]
  assert.match(gone(usage, 'chat-1'), /^You allowed your agent to send maps/)
  assert.equal(gone(usage, 'chat-2'), 'You said no to your agent using maps.')
})

test('a cleared refusal followed by a site added by hand claims no answer to the new ask', () => {
  // Refused, let ask again, asked again; the owner then added the site on the
  // key's page and the agent's next ask was simply granted, with no answer.
  const usage = [
    at('used', '2026-11-16T12:05:00Z', { caller: 'agent:chat-1' }),
    at('updated', '2026-11-16T12:00:00Z'),
    asked('chat-1', '2026-11-16T11:00:00Z'),
    at('forgot request agents', '2026-11-16T10:30:00Z'),
    at('denied agents', '2026-11-16T10:00:00Z'),
    asked('chat-1', '2026-11-16T09:59:00Z'),
  ]
  assert.equal(gone(usage), null)
})

test("without this chat's ask on record, outside a chat, or for an allowed key since deleted, nothing is claimed", () => {
  const answered = [at('granted agents', '2026-11-16T10:00:00Z'), asked('chat-1', '2026-11-16T09:59:00Z')]
  // The ask scrolled out of the activity fetched.
  assert.equal(gone([at('granted agents', '2026-11-16T10:00:00Z')]), null)
  assert.equal(gone(answered, null), null)
  assert.equal(gone(answered, 'chat-1', []), null)
  assert.equal(gone(undefined), null)
  // Another key's activity, or an app's answer, is not this request's.
  assert.equal(gone([at('granted agents', '2026-11-16T10:00:00Z', { key: 'other' }), asked('chat-1', '2026-11-16T09:59:00Z')]), null)
  assert.equal(gone([at('granted app:7', '2026-11-16T10:00:00Z'), asked('chat-1', '2026-11-16T09:59:00Z')]), null)
  assert.equal(goneKeyRequestLine('7-maps', 'chat-1', [mapsKey], answered), null)
})
