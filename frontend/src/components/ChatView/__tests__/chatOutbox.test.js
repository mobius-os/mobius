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
  inspectOutboxIntent,
  listIntents,
  outboxPrincipalKey,
  outboxRequestPath,
  resetOutboxReplaySessionForTests,
  retireIntent,
  storedIntentOwnership,
  subscribeOutboxSettlement,
} from '../chatOutbox.js'
import { sendDraftIdentity } from '../sendAttemptIdentity.js'
import { retireInteractiveIntent } from '../useStreamConnection.js'

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

  await enqueue({
    chatId: 'c1', cid: 'x1', type: 'message',
    body: { content: 'hello', cid: 'different' },
  })
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
    { chatId: 'c9', cid: 'x1', outcome: 'delivered' },
    { chatId: 'c9', cid: 'x2', outcome: 'failed' },
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
    { chatId: 'c9', cid: 'rejected-delete-fails', outcome: 'failed' },
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
    { chatId: 'c9', cid: 'accepted-delete-fails', outcome: 'delivered' },
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
    { chatId: 'c9', cid: 'retry', outcome: 'delivered' },
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
    { chatId: 'c9', cid: 'retry', outcome: 'delivered' },
    { chatId: 'c9', cid: 'auth', outcome: 'delivered' },
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
