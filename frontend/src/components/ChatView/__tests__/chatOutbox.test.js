import { test, beforeEach, afterEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'

globalThis.indexedDB = new IDBFactory()

import {
  REPLAY_TIMEOUT_MS,
  classifyReplayOutcome,
  clearChatOutbox,
  clearOutboxForTests,
  deliverIntent,
  drainOutbox,
  enqueueIntent,
  listIntents,
  outboxPrincipalKey,
  outboxRequestPath,
  retireIntent,
  storedIntentOwnership,
  subscribeOutboxDelivered,
} from '../chatOutbox.js'

const realLocalStorage = globalThis.localStorage

function tokenFor(claims = {}) {
  const payload = Buffer.from(JSON.stringify({ sub: 'owner', epoch: 3, ...claims }))
    .toString('base64url')
  return `stub.${payload}.stub`
}

function installToken(token = tokenFor()) {
  const values = new Map([['token', token]])
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => { values.set(key, String(value)) },
    removeItem: key => { values.delete(key) },
  }
}

function currentPrincipalKey() {
  return outboxPrincipalKey(globalThis.localStorage.getItem('token'))
}

function enqueue(record) {
  return enqueueIntent({ ...record, principalKey: currentPrincipalKey() })
}

function list() {
  return listIntents(currentPrincipalKey())
}

function mockRequest(handler) {
  const calls = []
  const request = async (record, options) => {
    calls.push({ record, options })
    return handler(calls.length, { record, options })
  }
  return { calls, request }
}

function drain(request) {
  return drainOutbox({
    deliver: record => deliverIntent(record, request),
    principalKey: currentPrincipalKey(),
  })
}

function httpResponse(status) {
  return { ok: status >= 200 && status < 300, status, json: async () => ({}) }
}

beforeEach(async () => {
  installToken()
  globalThis.indexedDB = new IDBFactory()
  await clearOutboxForTests()
})

afterEach(() => {
  globalThis.localStorage = realLocalStorage
})

test('classifyReplayOutcome separates delivery, transient, auth, and terminal responses', () => {
  assert.equal(classifyReplayOutcome({ ok: true, status: 202 }), 'delivered')
  assert.equal(classifyReplayOutcome({ ok: false, status: 410 }), 'delivered')
  assert.equal(classifyReplayOutcome({ ok: false, status: 401 }), 'auth')
  assert.equal(classifyReplayOutcome({ ok: false, status: 403 }), 'auth')
  assert.equal(classifyReplayOutcome({ ok: false, status: 408 }), 'retry')
  assert.equal(classifyReplayOutcome({ ok: false, status: 425 }), 'retry')
  assert.equal(classifyReplayOutcome({ ok: false, status: 429 }), 'retry')
  assert.equal(classifyReplayOutcome({ ok: false, status: 503 }), 'retry')
  assert.equal(classifyReplayOutcome({ ok: false, status: 400 }), 'failed')
  assert.equal(classifyReplayOutcome({ ok: false, status: 409 }), 'failed')
})

test('principal keys survive owner token renewal but bind embedded chat capability', () => {
  assert.equal(
    outboxPrincipalKey(tokenFor({ exp: 100 })),
    outboxPrincipalKey(tokenFor({ exp: 200 })),
  )
  assert.notEqual(
    outboxPrincipalKey(tokenFor()),
    outboxPrincipalKey(tokenFor({ scope: 'chat_embed', app_id: 4, chat_id: 'c1' })),
  )
  assert.notEqual(
    outboxPrincipalKey(tokenFor({ scope: 'chat_embed', app_id: 4, chat_id: 'c1' })),
    outboxPrincipalKey(tokenFor({ scope: 'chat_embed', app_id: 4, chat_id: 'c2' })),
  )
  assert.equal(outboxPrincipalKey('opaque'), null)
})

test('a queued intent survives to disk and is listable only by its principal', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', type: 'message', body: { content: 'hi', cid: 'x1' } })
  assert.equal((await list()).length, 1)

  installToken(tokenFor({ sub: 'another-owner' }))
  assert.equal((await list()).length, 0)
  installToken()
  assert.equal((await list()).length, 0, 'mismatched owner data is pruned')
})

test('unscoped legacy intent is discarded instead of adopted by this owner', () => {
  assert.equal(storedIntentOwnership(null, currentPrincipalKey()), 'discard')
})

test('same-owner embedded capabilities do not delete each other\'s queued text', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'owner', cid: 'x1' } })
  installToken(tokenFor({ scope: 'chat_embed', app_id: 4, chat_id: 'c2' }))
  assert.equal((await list()).length, 0, 'the embed cannot replay the owner record')

  installToken()
  assert.equal((await list()).length, 1, 'filtering one capability did not erase another')
})

test('the same cid enqueued twice stays one intent', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', type: 'message', body: { content: 'a', cid: 'x1' } })
  const createdAt = (await list())[0].createdAt
  await enqueue({ chatId: 'c1', cid: 'x1', type: 'message', body: { content: 'a', cid: 'x1' } })
  assert.equal((await list()).length, 1)
  assert.equal((await list())[0].createdAt, createdAt, 'an idempotent retry keeps queue order')
})

test('the owning store clears queued owner text without a blocked database delete', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'private', cid: 'x1' } })
  assert.equal(await clearChatOutbox(), true)
  assert.equal((await list()).length, 0)
})

test('a request already in flight cannot resurrect intent after owner cleanup', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'private', cid: 'x1' } })
  let resolveRequest
  const request = () => new Promise(resolve => { resolveRequest = resolve })
  const draining = drain(request)
  for (let index = 0; index < 20 && !resolveRequest; index += 1) {
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  assert.equal(typeof resolveRequest, 'function')

  await clearChatOutbox()
  resolveRequest(httpResponse(403))
  await draining
  assert.equal((await list()).length, 0)
})

test('drain URL-encodes the chat id, reuses the cid, and retires on accept', async () => {
  await enqueue({
    chatId: 'chat/with space', cid: 'keepme', type: 'message',
    body: { content: 'hello', cid: 'keepme' },
  })
  const { calls, request } = mockRequest(() => httpResponse(202))
  await drain(request)
  assert.equal(calls.length, 1)
  assert.equal(outboxRequestPath(calls[0].record.chatId), '/chats/chat%2Fwith%20space/messages')
  assert.equal(calls[0].record.body.cid, 'keepme')
  assert.equal((await list()).length, 0)
})

test('a replay timeout resolves to retry instead of wedging the drain', async () => {
  const request = (_record, options) => new Promise((_resolve, reject) => {
    options?.signal?.addEventListener('abort', () => {
      reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
    })
  })
  mock.timers.enable({ apis: ['setTimeout'] })
  try {
    const delivering = deliverIntent({
      chatId: 'c1', body: { content: 'hi', cid: 'x1' },
    }, request)
    mock.timers.tick(REPLAY_TIMEOUT_MS)
    assert.equal(await delivering, 'retry')
  } finally {
    mock.timers.reset()
  }
})

test('one transport failure preserves order and stops the drain burst', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'one', cid: 'x1' } })
  await enqueue({ chatId: 'c1', cid: 'x2', body: { content: 'two', cid: 'x2' } })
  const { calls, request } = mockRequest(() => { throw new Error('offline') })
  await drain(request)
  assert.equal(calls.length, 1)
  assert.equal((await list()).length, 2)
})

test('an auth rejection is kept but attempted only once per loaded document', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'hi', cid: 'x1' } })
  const { calls, request } = mockRequest(() => httpResponse(403))
  await drain(request)
  await drain(request)
  assert.equal(calls.length, 1)
  assert.equal((await list()).length, 1)
})

test('an already-resolved answer is delivered while a permanent rejection retires', async () => {
  await enqueue({
    chatId: 'c1', cid: 'ans1', type: 'answer',
    body: { content: 'x', cid: 'ans1', answers: {}, question_id: 'q1' },
  })
  const resolved = mockRequest(() => httpResponse(410))
  await drain(resolved.request)
  assert.equal((await list()).length, 0)

  await enqueue({ chatId: 'c1', cid: 'bad', body: { content: '', cid: 'bad' } })
  const rejected = mockRequest(() => httpResponse(400))
  await drain(rejected.request)
  assert.equal((await list()).length, 0)
})

test('delivery announces the chat id so a mounted chat can reconcile', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxDelivered(id => seen.push(id))
  await enqueue({ chatId: 'c9', cid: 'x1', body: { content: 'hi', cid: 'x1' } })
  const accepted = mockRequest(() => httpResponse(202))
  await drain(accepted.request)
  unsubscribe()
  assert.deepEqual(seen, ['c9'])
})

test('concurrent drains are single-flight', async () => {
  await enqueue({ chatId: 'c1', cid: 'x1', body: { content: 'hi', cid: 'x1' } })
  let resolveFirst
  const gate = new Promise(resolve => { resolveFirst = resolve })
  const { calls, request } = mockRequest(async () => { await gate; return httpResponse(202) })
  const first = drain(request)
  const second = drain(request)
  resolveFirst()
  await Promise.all([first, second])
  assert.equal(calls.length, 1)
  await retireIntent('x1')
})
