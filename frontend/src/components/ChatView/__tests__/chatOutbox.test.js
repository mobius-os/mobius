import { test, beforeEach, afterEach, mock } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory, IDBObjectStore } from 'fake-indexeddb'
import { createStore, set } from 'idb-keyval'

globalThis.indexedDB = new IDBFactory()

import {
  REPLAY_TIMEOUT_MS,
  classifyReplayOutcome,
  clearChatOutbox,
  clearOutboxForTests,
  deliverIntent,
  drainOutbox,
  enqueueIntent,
  inspectOutboxIntent,
  listIntents,
  outboxPrincipalKey,
  outboxRequestPath,
  resetOutboxReplaySessionForTests,
  retireIntent,
  storedIntentOwnership,
  subscribeOutboxSettlement,
  subscribeOutboxChanges,
  markIntentLocallyQueued,
  claimIntentDispatch,
  cancelLocalIntent,
  editLocalIntent,
} from '../chatOutbox.js'
import { sendDraftIdentity } from '../sendAttemptIdentity.js'
import { retireInteractiveIntent } from '../useStreamConnection.js'

const realLocalStorage = globalThis.localStorage
const realNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')

function installReplayLocks() {
  let held = false
  const calls = []
  const locks = {
    calls,
    request(name, options, callback) {
      if (typeof options === 'function') { callback = options; options = {} }
      calls.push({ name, options })
      if (held) {
        if (options.ifAvailable) return Promise.resolve(callback(null))
        throw new Error('unexpected nested replay lock request')
      }
      held = true
      try {
        return Promise.resolve(callback({ name })).finally(() => { held = false })
      } catch (error) {
        held = false
        return Promise.reject(error)
      }
    },
  }
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true, value: { locks },
  })
  return locks
}

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

function failedAttempt(
  chatId,
  cid,
  text,
  attachments = [],
  transportContent = text,
) {
  return {
    cid,
    text,
    transportContent,
    attachments,
    draftIdentity: sendDraftIdentity(chatId, text, attachments),
  }
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
  installReplayLocks()
  await clearOutboxForTests()
})

afterEach(() => {
  globalThis.localStorage = realLocalStorage
  if (realNavigator) Object.defineProperty(globalThis, 'navigator', realNavigator)
  else delete globalThis.navigator
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

test('exact intent inspection accepts ordinary raw content and remains side-effect-free', async () => {
  const principalKey = currentPrincipalKey()
  const attempt = failedAttempt('c1', 'x1', 'hello')
  await enqueue({
    chatId: 'c1', cid: 'x1', type: 'message',
    body: { content: 'hello', cid: 'x1' },
  })
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'x1', principalKey, attempt,
  }), 'retained')
  assert.equal(await inspectOutboxIntent({
    chatId: 'c2', cid: 'x1', principalKey, attempt,
  }), 'absent')
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'x1', principalKey: null, attempt,
  }), 'unknown')
  assert.equal((await list()).length, 1, 'inspection never prunes the retained row')

  // Seed malformed persisted data directly: idempotent enqueue now preserves
  // an existing body's identity instead of serving as a corruption helper.
  await set('x1', {
    ...(await list())[0], body: { content: 'hello', cid: 'different' },
  }, createStore('mobius-chat-outbox', 'intents-v1'))
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'x1', principalKey, attempt,
  }), 'absent')
  assert.equal((await list()).length, 1, 'a malformed body is observed, not rewritten')
})

test('exact intent inspection rejects the same cid with different visible content', async () => {
  const principalKey = currentPrincipalKey()
  await enqueue({
    chatId: 'c1', cid: 'same-cid', type: 'message',
    body: { content: 'different body', cid: 'same-cid' },
  })
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1',
    cid: 'same-cid',
    principalKey,
    attempt: failedAttempt('c1', 'same-cid', 'restored body'),
  }), 'absent')
  assert.equal((await list()).length, 1, 'mismatch inspection remains read-only')
})

test('exact intent inspection rejects attachment and draft identity mismatches', async () => {
  const principalKey = currentPrincipalKey()
  const restoredAttachments = [
    { name: 'notes.txt', size: 12, mime_type: 'text/plain' },
  ]
  await enqueue({
    chatId: 'c1', cid: 'file-cid', type: 'message',
    body: {
      content: 'with file',
      cid: 'file-cid',
      attachments: [{ name: 'notes.txt', size: 13, mime_type: 'text/plain' }],
    },
  })
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1',
    cid: 'file-cid',
    principalKey,
    attempt: failedAttempt('c1', 'file-cid', 'with file', restoredAttachments),
  }), 'absent')

  const tampered = failedAttempt('c1', 'file-cid', 'with file', restoredAttachments)
  tampered.draftIdentity = sendDraftIdentity('c1', 'another draft', restoredAttachments)
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'file-cid', principalKey, attempt: tampered,
  }), 'absent')
})

test('an answer intent cannot masquerade as a failed visible message', async () => {
  const principalKey = currentPrincipalKey()
  await enqueue({
    chatId: 'c1', cid: 'answer-cid', type: 'answer',
    body: {
      content: 'answer text',
      cid: 'answer-cid',
    },
  })
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1',
    cid: 'answer-cid',
    principalKey,
    attempt: failedAttempt('c1', 'answer-cid', 'answer text'),
  }), 'absent')
})

test('exact intent inspection accepts the exact app context and ignores incidental request fields', async () => {
  const principalKey = currentPrincipalKey()
  const attachments = [
    { name: 'notes.txt', size: 12, mime_type: 'text/plain' },
  ]
  const transportContent =
    'visible text\n\n<app_state>\n  <selection>row 4</selection>\n</app_state>'
  const attempt = failedAttempt(
    'c1', 'augmented', 'visible text', attachments, transportContent,
  )
  await enqueue({
    chatId: 'c1', cid: 'augmented', type: 'message',
    body: {
      content: transportContent,
      cid: 'augmented',
      attachments,
      timezone: 'UTC',
      viewport: { width: 390, height: 700, devicePixelRatio: 3 },
    },
  })
  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'augmented', principalKey, attempt,
  }), 'retained')
})

test('exact intent inspection rejects altered app context for the same visible draft', async () => {
  const principalKey = currentPrincipalKey()
  const attachments = [
    { name: 'notes.txt', size: 12, mime_type: 'text/plain' },
  ]
  const attempt = failedAttempt(
    'c1',
    'augmented-mismatch',
    'visible text',
    attachments,
    'visible text\n\n<app_state>\n  <selection>row 4</selection>\n</app_state>',
  )
  await enqueue({
    chatId: 'c1', cid: 'augmented-mismatch', type: 'message',
    body: {
      content: 'visible text\n\n<app_state>\n  <selection>row 5</selection>\n</app_state>',
      cid: 'augmented-mismatch',
      attachments,
    },
  })

  assert.equal(await inspectOutboxIntent({
    chatId: 'c1', cid: 'augmented-mismatch', principalKey, attempt,
  }), 'absent')
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

test('terminal retirement announces the exact cid and outcome so one draft can reconcile', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({ chatId: 'c9', cid: 'x1', body: { content: 'hi', cid: 'x1' } })
  const accepted = mockRequest(() => httpResponse(202))
  await drain(accepted.request)
  await enqueue({ chatId: 'c9', cid: 'x2', body: { content: 'bad', cid: 'x2' } })
  const rejected = mockRequest(() => httpResponse(400))
  await drain(rejected.request)
  unsubscribe()
  assert.deepEqual(seen, [
    { chatId: 'c9', cid: 'x1', type: 'message', outcome: 'delivered' },
    { chatId: 'c9', cid: 'x2', type: 'message', outcome: 'failed' },
  ])
})

test('mismatched-chat retirement leaves the owned row replayable and unsettled', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({
    chatId: 'owned-chat', cid: 'shared-cid', type: 'message',
    body: { content: 'keep me', cid: 'shared-cid' },
  })
  const before = await list()

  assert.equal(await retireIntent(
    'shared-cid',
    { chatId: 'other-chat', outcome: 'delivered' },
  ), true)
  assert.deepEqual(await list(), before, 'the mismatched row is not mutated')
  assert.deepEqual(seen, [], 'the mismatched row is not settled')
  unsubscribe()

  const replay = mockRequest(() => httpResponse(202))
  await drain(replay.request)
  assert.equal(replay.calls.length, 1, 'the owned row remains eligible for replay')
})

test('interactive terminal outcomes stay chat-scoped when enqueue retention is false', async () => {
  for (const outcome of ['delivered', 'failed']) {
    const calls = []
    const outboxRetained = await retireInteractiveIntent({
      cid: `${outcome}-cid`,
      chatId: `${outcome}-chat`,
      outcome,
      outboxRetained: false,
      retire: async (...args) => {
        calls.push(args)
        return true
      },
    })

    assert.equal(outboxRetained, false)
    assert.deepEqual(calls, [[
      `${outcome}-cid`,
      { chatId: `${outcome}-chat`, outcome },
    ]])
  }
})

test('interactive retirement reports durable retention honestly on transition failure', async () => {
  assert.equal(await retireInteractiveIntent({
    cid: 'retained-cid',
    chatId: 'owned-chat',
    outcome: 'failed',
    outboxRetained: true,
    retire: async () => false,
  }), true)
  assert.equal(await retireInteractiveIntent({
    cid: 'unretained-cid',
    chatId: 'owned-chat',
    outcome: 'failed',
    outboxRetained: false,
    retire: async () => false,
  }), false)
})

const failRetiredCleanup = async () => {
  throw new Error('injected delete failure')
}

test('terminal rejection stays non-replayable when retired-row deletion fails', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({
    chatId: 'c9', cid: 'rejected-delete-fails', type: 'message',
    body: { content: 'invalid', cid: 'rejected-delete-fails' },
  })

  assert.equal(await retireIntent(
    'rejected-delete-fails',
    { chatId: 'c9', outcome: 'failed' },
    { cleanup: failRetiredCleanup },
  ), true)
  const replay = mockRequest(() => httpResponse(202))
  await drain(replay.request)
  unsubscribe()

  assert.equal(replay.calls.length, 0)
  assert.deepEqual(seen, [
    { chatId: 'c9', cid: 'rejected-delete-fails', type: 'message', outcome: 'failed' },
  ])
})

test('accepted response stays duplicate-safe and settles when retired-row deletion fails', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({
    chatId: 'c9', cid: 'accepted-delete-fails', type: 'message',
    body: { content: 'accepted', cid: 'accepted-delete-fails' },
  })

  assert.equal(await retireIntent(
    'accepted-delete-fails',
    { chatId: 'c9', outcome: 'delivered' },
    { cleanup: failRetiredCleanup },
  ), true)
  const replay = mockRequest(() => httpResponse(202))
  await drain(replay.request)
  unsubscribe()

  assert.equal(replay.calls.length, 0, 'accepted cid is not posted again')
  assert.deepEqual(seen, [
    { chatId: 'c9', cid: 'accepted-delete-fails', type: 'message', outcome: 'delivered' },
  ])
})

test('user cancellation stays non-replayable and emits no settlement when deletion fails', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({
    chatId: 'c9', cid: 'cancelled-delete-fails', type: 'message',
    body: { content: 'cancel me', cid: 'cancelled-delete-fails' },
  })

  assert.equal(await retireIntent(
    'cancelled-delete-fails',
    { chatId: 'c9', outcome: 'cancelled' },
    { cleanup: failRetiredCleanup },
  ), true)
  const replay = mockRequest(() => httpResponse(202))
  await drain(replay.request)
  unsubscribe()

  assert.equal(replay.calls.length, 0)
  assert.deepEqual(seen, [], 'cancellation is not a delivery/failure settlement')
})

test('retryable transport and auth outcomes retain intent without terminal settlement', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  await enqueue({ chatId: 'c9', cid: 'retry', body: { content: 'later', cid: 'retry' } })
  await drain(mockRequest(() => httpResponse(503)).request)
  assert.deepEqual(seen, [])
  assert.equal((await list()).length, 1)
  const retryReplay = mockRequest(() => httpResponse(202))
  await drain(retryReplay.request)
  assert.equal(retryReplay.calls.length, 1, 'transient intent remains replayable')
  assert.deepEqual(seen, [
    { chatId: 'c9', cid: 'retry', type: 'message', outcome: 'delivered' },
  ])

  await enqueue({ chatId: 'c9', cid: 'auth', body: { content: 'owner', cid: 'auth' } })
  await drain(mockRequest(() => httpResponse(403)).request)
  assert.equal(seen.length, 1, 'auth rejection stays silent')
  assert.equal((await list()).length, 1)
  resetOutboxReplaySessionForTests()
  const authReplay = mockRequest(() => httpResponse(202))
  await drain(authReplay.request)
  unsubscribe()
  assert.equal(authReplay.calls.length, 1, 'a new authenticated document can replay it')
  assert.deepEqual(seen, [
    { chatId: 'c9', cid: 'retry', type: 'message', outcome: 'delivered' },
    { chatId: 'c9', cid: 'auth', type: 'message', outcome: 'delivered' },
  ])
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

test('quiet answer replay retires without a message row and announces answer-owned reconciliation', async () => {
  const seen = []
  const unsubscribe = subscribeOutboxSettlement(settlement => seen.push(settlement))
  const body = {
    content: '- Anything else?: No', cid: 'quiet-answer', hidden: true,
    answers: { 'Anything else?': 'No' }, question_id: 'saved-card', selected_options: { help: ['0'] },
  }
  await enqueue({ chatId: 'c1', cid: 'quiet-answer', type: 'answer', body })
  const accepted = mockRequest(() => ({
    ok: true, status: 200,
    json: async () => ({ status: 'answered', answer_turn: 'none', running: false }),
  }))
  await drain(accepted.request)
  unsubscribe()
  assert.deepEqual(accepted.calls[0].record.body, body, 'replay preserves explicit ids and hidden intent')
  assert.deepEqual(await list(), [])
  assert.deepEqual(seen, [{ chatId: 'c1', cid: 'quiet-answer', type: 'answer', outcome: 'delivered' }])
})

test('projection changes publish only committed ownership-bound records and retirement', async () => {
  const changes = []
  const stop = subscribeOutboxChanges(change => changes.push(change))
  try {
    await enqueue({ chatId: 'local-chat', cid: 'local-cid', body: { cid: 'local-cid', content: 'local' } })
    assert.equal(changes[0].kind, 'enqueue')
    assert.equal(changes[0].record.locallyQueued, false)
    assert.equal(changes[0].requestDelivery, false)
    assert.equal((await list())[0].cid, changes[0].record.cid)
    assert.equal(await markIntentLocallyQueued('local-cid', {
      chatId: 'wrong-chat', principalKey: currentPrincipalKey(),
    }), false)
    assert.equal(await markIntentLocallyQueued('local-cid', {
      chatId: 'local-chat', principalKey: 'wrong-principal',
    }), false)
    assert.equal(changes.length, 1, 'foreign callers cannot modify or announce the stored intent')
    assert.equal(await markIntentLocallyQueued('local-cid', {
      chatId: 'local-chat', principalKey: currentPrincipalKey(),
    }), true)
    assert.equal(changes[1].record.locallyQueued, true)
    assert.notEqual(changes[1].requestDelivery, true, 'a transport failure does not trigger its own retry')
    assert.equal((await list())[0].locallyQueued, true)
    await retireIntent('local-cid', { chatId: 'local-chat', outcome: 'cancelled' })
    assert.equal(changes[2].kind, 'retire')
    assert.equal(changes[2].outcome, 'cancelled')
    assert.deepEqual(await list(), [])
    await clearChatOutbox()
    assert.equal(changes[3].kind, 'clear')
  } finally {
    stop()
  }
})

test('marking deferred presentation never recreates a missing or retired intent', async () => {
  const options = { chatId: 'chat', principalKey: currentPrincipalKey() }
  assert.equal(await markIntentLocallyQueued('absent', options), false)
  await enqueue({ chatId: 'chat', cid: 'retired', body: { cid: 'retired', content: 'done' } })
  await retireIntent('retired', { chatId: 'chat', outcome: 'delivered' }, {
    cleanup: async () => { throw new Error('delete unavailable') },
  })
  assert.equal(await markIntentLocallyQueued('retired', options), false)
  assert.deepEqual(await list(), [])
})

test('a follow-up behind an unresolved question stays durable when its queued answer fails', async () => {
  await enqueue({ chatId: 'chat', cid: 'answer-a', type: 'answer', body: { question_id: 'question', answers: {} } })
  await enqueue({ chatId: 'chat', cid: 'follow-up-b', body: { content: 'Please do B next', cid: 'follow-up-b' } })
  const { request, calls } = mockRequest((_index, { record }) => (
    record.type === 'answer'
      ? httpResponse(422)
      : { ...httpResponse(409), json: async () => ({ detail: { code: 'pending_question_open' } }) }
  ))
  await drain(request)
  assert.equal(calls.length, 2)
  assert.deepEqual((await list()).map(record => record.cid), ['follow-up-b'])
  const accepted = mockRequest(() => httpResponse(202))
  await drain(accepted.request)
  assert.equal(accepted.calls[0].record.cid, 'follow-up-b')
  assert.deepEqual(await list(), [])
})

test('question admission blocks follow-ups without being confused with transport failure', async () => {
  assert.equal(classifyReplayOutcome({ ok: false, status: 409, code: 'pending_question_open' }), 'question_blocked')
  assert.equal(classifyReplayOutcome({ ok: false, status: 409, code: 'cid_conflict' }), 'failed')
  const record = { chatId: 'chat', cid: 'message', body: { content: 'message' } }
  assert.equal(await deliverIntent(record, async () => ({
    ...httpResponse(409), json: async () => ({ detail: { code: 'cid_conflict' } }),
  })), 'failed')
})


test('only a newly submitted deferred intent requests delivery from change listeners', async () => {
  const changes = []
  const stop = subscribeOutboxChanges(change => changes.push(change))
  try {
    await enqueue({ chatId: 'chat', cid: 'deferred', locallyQueued: true, body: { cid: 'deferred', content: 'later' } })
    assert.equal(changes[0].requestDelivery, true)
    await markIntentLocallyQueued('deferred', { chatId: 'chat', principalKey: currentPrincipalKey() })
    assert.notEqual(changes[1].requestDelivery, true)
  } finally { stop() }
})

function localOwner(chatId = 'local') {
  return { chatId, principalKey: currentPrincipalKey() }
}
function queuedRecord(cid = 'q', body = {}) {
  return { chatId: 'local', cid, locallyQueued: true, body: { cid, content: 'original', ...body } }
}

test('never-dispatched cancellation wins atomically before a captured replay can POST', async () => {
  await enqueue(queuedRecord())
  const captured = (await list())[0]
  assert.equal(captured.dispatchStarted, false)
  const [cancel, claim] = await Promise.all([
    cancelLocalIntent('q', localOwner()),
    claimIntentDispatch('q', localOwner()),
  ])
  assert.equal(cancel.status, 'cancelled')
  assert.ok(['retired', 'absent'].includes(claim.status))
  const { calls, request } = mockRequest(() => httpResponse(202))
  await drain(request)
  assert.equal(calls.length, 0)
  assert.deepEqual(await list(), [])
})

test('dispatch claim wins atomically and local cancellation cannot claim uncertain server work', async () => {
  await enqueue(queuedRecord())
  const [claim, cancel] = await Promise.all([
    claimIntentDispatch('q', localOwner()),
    cancelLocalIntent('q', localOwner()),
  ])
  assert.equal(claim.status, 'claimed')
  assert.equal(claim.record.dispatchStarted, true)
  assert.equal(cancel.status, 'uncertain')
  assert.equal((await list())[0].dispatchStarted, true)
  assert.equal((await editLocalIntent('q', { ...localOwner(), content: 'replacement' })).status, 'uncertain')
})

test('plain local edit preserves attachments and metadata and dispatch reads the committed edit', async () => {
  const attachments = [{ name: 'photo.png', url: '/api/files/photo.png', size: 30 }]
  await enqueue(queuedRecord('q', { attachments, viewport: { width: 390 }, timezone: 'Europe/London' }))
  const before = (await list())[0]
  const [edit, claim] = await Promise.all([
    editLocalIntent('q', { ...localOwner(), content: ' edited text ' }),
    claimIntentDispatch('q', localOwner()),
  ])
  assert.equal(edit.status, 'saved')
  assert.equal(claim.status, 'claimed')
  assert.deepEqual(claim.record.body, { ...before.body, content: 'edited text' })
  assert.equal(claim.record.cid, before.cid)
  assert.equal(claim.record.createdAt, before.createdAt)
  await enqueue(queuedRecord('q', { content: 'stale captured original' }))
  assert.equal((await list())[0].body.content, 'edited text', 'sameCID re-enqueue cannot silently overwrite the canonical body')
  assert.equal((await list())[0].dispatchStarted, true, 'retry cannot restore a never-dispatched proof')
})

test('local edit rejects augmented and answer bodies without losing hidden context', async () => {
  for (const content of [
    'original\n\n<app_state>context</app_state>',
    'original\n\n<agent_experience>context</agent_experience>',
    'original\n[Files in this session:\nphoto.png]',
  ]) {
    await clearOutboxForTests()
    await enqueue(queuedRecord('q', { content }))
    assert.equal((await editLocalIntent('q', { ...localOwner(), content: 'edited' })).status, 'unsupported')
    assert.equal((await list())[0].body.content, content)
  }
  await clearOutboxForTests()
  await enqueue({ ...queuedRecord('answer'), type: 'answer', body: { answers: { choice: 'Yes' }, question_id: 'card' } })
  assert.equal((await editLocalIntent('answer', { ...localOwner(), content: 'edited' })).status, 'unsupported')
})

test('legacy intents have no never-dispatched proof and remain replayable without unsafe mutation', async () => {
  const record = { ...queuedRecord('legacy'), principalKey: currentPrincipalKey(), createdAt: 1 }
  await set('legacy', record, createStore('mobius-chat-outbox', 'intents-v1'))
  assert.equal((await cancelLocalIntent('legacy', localOwner())).status, 'uncertain')
  assert.equal((await editLocalIntent('legacy', { ...localOwner(), content: 'edited' })).status, 'uncertain')
  const claim = await claimIntentDispatch('legacy', localOwner())
  assert.equal(claim.status, 'claimed')
  assert.deepEqual(claim.record.body, record.body)
  assert.equal(claim.record.dispatchStarted, true)
})

test('dispatch, edit and local cancel cannot cross owner or chat boundaries', async () => {
  await enqueue(queuedRecord())
  for (const owner of [localOwner('other-chat'), { ...localOwner(), principalKey: 'other-owner' }]) {
    assert.equal((await claimIntentDispatch('q', owner)).status, 'not_owned')
    assert.equal((await editLocalIntent('q', { ...owner, content: 'foreign edit' })).status, 'not_local')
    assert.equal((await cancelLocalIntent('q', owner)).status, 'not_local')
  }
  const retained = (await list())[0]
  assert.equal(retained.body.content, 'original')
  assert.equal(retained.dispatchStarted, false)
})

test('a late authorization response never recreates a retired intent', async () => {
  await enqueue(queuedRecord())
  let resolveRequest
  const draining = drain(() => new Promise(resolve => { resolveRequest = resolve }))
  for (let index = 0; index < 30 && !resolveRequest; index += 1) {
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  assert.equal(typeof resolveRequest, 'function')
  await retireIntent('q', { chatId: 'local', outcome: 'cancelled' })
  resolveRequest(httpResponse(403))
  await draining
  assert.deepEqual(await list(), [])
})

test('active replay refuses local mutations rather than invalidate an older tab snapshot', async () => {
  await enqueue(queuedRecord('a'))
  await enqueue(queuedRecord('b'))
  await enqueue(queuedRecord('c'))
  let releaseFirst
  const sent = []
  const draining = drain(async record => {
    sent.push(record)
    if (record.cid === 'a') await new Promise(resolve => { releaseFirst = resolve })
    return httpResponse(202)
  })
  for (let index = 0; index < 30 && !releaseFirst; index += 1) {
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  assert.equal(typeof releaseFirst, 'function')
  assert.deepEqual(await cancelLocalIntent('b', localOwner()), { status: 'uncertain', reason: 'replay_busy' })
  assert.deepEqual(await editLocalIntent('c', { ...localOwner(), content: 'new C' }), { status: 'uncertain', reason: 'replay_busy' })
  releaseFirst()
  await draining
  assert.deepEqual(sent.map(record => record.cid), ['a', 'b', 'c'])
  assert.equal(sent[2].body.content, 'original', 'the refused edit never claims to change the replay body')
  assert.deepEqual(await list(), [])
})

test('unavailable local ownership transactions report failure without claiming cancellation or dispatch', async () => {
  await enqueue(queuedRecord())
  const failure = mock.method(IDBObjectStore.prototype, 'get', () => { throw new Error('IndexedDB unavailable') })
  try {
    assert.equal((await claimIntentDispatch('q', localOwner())).status, 'unavailable')
    assert.equal((await cancelLocalIntent('q', localOwner())).status, 'unavailable')
    assert.equal((await editLocalIntent('q', { ...localOwner(), content: 'edited' })).status, 'unavailable')
  } finally { failure.mock.restore() }
  const retained = (await list())[0]
  assert.equal(retained.dispatchStarted, false)
  assert.equal(retained.body.content, 'original')
})

test('local mutations use the existing free replay lock without queueing or nesting dispatch claims', async () => {
  await enqueue(queuedRecord())
  const locks = navigator.locks
  assert.equal((await editLocalIntent('q', { ...localOwner(), content: 'edited' })).status, 'saved')
  assert.equal((await cancelLocalIntent('q', localOwner())).status, 'cancelled')
  assert.deepEqual(locks.calls, [
    { name: 'mobius-chat-outbox', options: { ifAvailable: true } },
    { name: 'mobius-chat-outbox', options: { ifAvailable: true } },
  ])
  await enqueue(queuedRecord('dispatch'))
  await locks.request('mobius-chat-outbox', async () => {
    assert.equal((await claimIntentDispatch('dispatch', localOwner())).status, 'claimed')
  })
  assert.equal(locks.calls.length, 3, 'claim does not reacquire the drain-owned lock')
})

test('a pre-upgrade tab holding a snapshot blocks local mutations immediately without changing intent', async () => {
  await enqueue(queuedRecord())
  let releaseOldTab
  let captured
  const oldTab = navigator.locks.request('mobius-chat-outbox', async () => {
    captured = (await list())[0]
    await new Promise(resolve => { releaseOldTab = resolve })
  })
  for (let index = 0; index < 30 && !releaseOldTab; index += 1) {
    await new Promise(resolve => setTimeout(resolve, 0))
  }
  assert.equal(typeof releaseOldTab, 'function')
  // The old implementation never changes dispatchStarted before POST.
  assert.equal(captured.dispatchStarted, false)
  assert.deepEqual(await editLocalIntent('q', { ...localOwner(), content: 'unsafe edit' }), {
    status: 'uncertain', reason: 'replay_busy',
  })
  assert.deepEqual(await cancelLocalIntent('q', localOwner()), {
    status: 'uncertain', reason: 'replay_busy',
  })
  assert.deepEqual((await list())[0], captured)
  releaseOldTab()
  await oldTab
  assert.equal((await editLocalIntent('q', { ...localOwner(), content: 'safe edit' })).status, 'saved')
  assert.equal((await cancelLocalIntent('q', localOwner())).status, 'cancelled')
})

test('without Web Locks local mutation stays conservative while replay remains available', async () => {
  await enqueue(queuedRecord())
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: {} })
  const before = (await list())[0]
  assert.deepEqual(await editLocalIntent('q', { ...localOwner(), content: 'unsafe' }), {
    status: 'uncertain', reason: 'replay_lock_unavailable',
  })
  assert.deepEqual(await cancelLocalIntent('q', localOwner()), {
    status: 'uncertain', reason: 'replay_lock_unavailable',
  })
  assert.deepEqual((await list())[0], before)
  const { calls, request } = mockRequest(() => httpResponse(202))
  await drain(request)
  assert.equal(calls.length, 1)
  assert.deepEqual(await list(), [])
})


test('a blocked follow-up cannot starve its corrected answer or another chat', async t => {
  let timestamp = 1000
  t.mock.method(Date, 'now', () => timestamp++)
  for (const [cid, type, chatId] of [
    ['rejected-answer', 'answer', 'chat'],
    ['follow-up-b', 'message', 'chat'],
    ['follow-up-d', 'message', 'chat'],
    ['corrected-answer', 'answer', 'chat'],
    ['unrelated', 'message', 'other'],
  ]) await enqueue({ chatId, cid, type, body: { cid, content: cid } })
  let answered = false
  const { request, calls } = mockRequest((_index, { record }) => {
    if (record.cid === 'rejected-answer') return httpResponse(422)
    if (record.cid === 'corrected-answer') answered = true
    if (record.type === 'message' && record.chatId === 'chat' && !answered) {
      return { ...httpResponse(409), json: async () => ({ detail: { code: 'pending_question_open' } }) }
    }
    return httpResponse(202)
  })
  await drain(request)
  assert.deepEqual(calls.map(call => call.record.cid), [
    'rejected-answer', 'follow-up-b', 'corrected-answer', 'unrelated',
  ])
  assert.deepEqual((await list()).map(record => record.cid), ['follow-up-b', 'follow-up-d'])
  await drain(request)
  assert.deepEqual(calls.slice(-2).map(call => call.record.cid), ['follow-up-b', 'follow-up-d'])
  assert.deepEqual(await list(), [])
})
