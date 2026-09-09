// Saved close answers must acknowledge durable intent without manufacturing stream activity.
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import { renderHook } from './react-hook-shim.mjs'
import useStreamConnection from '../../useStreamConnection.js'
import { shouldRepairRuntimeStream } from '../../chatRuntimeState.js'
import { clearOutboxForTests, listIntents, outboxPrincipalKey } from '../../chatOutbox.js'
import { writeStoredStreamSnapshot } from '../../streamSnapshotCache.js'

const original = Object.fromEntries(
  ['window', 'document', 'sessionStorage', 'fetch', 'localStorage', 'indexedDB'].map(key => [key, globalThis[key]]),
)
let hook
let token

afterEach(() => {
  hook?.unmount()
  hook = null
  for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete globalThis[key]
    else globalThis[key] = value
  }
})

async function setup(callbacks = {}) {
  globalThis.window = Object.assign(new EventTarget(), { innerHeight: 800 })
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const values = new Map()
  globalThis.sessionStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  token = `stub.${Buffer.from(JSON.stringify({ sub: 'owner', epoch: 3 })).toString('base64url')}.stub`
  globalThis.localStorage = { getItem: key => key === 'token' ? token : null }
  globalThis.indexedDB = new IDBFactory()
  await clearOutboxForTests()
  writeStoredStreamSnapshot('a', {
    items: [{ type: 'text', content: 'Existing answer' }], assistantMessageId: 'assistant-a',
  })
  hook = renderHook(chat => useStreamConnection(chat, callbacks), 'a')
}

const answerOptions = {
  hidden: true, cid: 'answer-cid', question_id: 'saved-card',
  answers: { 'Anything else?': 'No' }, selected_options: { help: ['0'] },
}

for (const running of [false, true]) {
  test(`quiet answer with running=${running} keeps current content without a stream request`, async () => {
    await setup()
    const requests = []
    globalThis.fetch = async (url, options) => {
      requests.push({ url, ...options })
      return Response.json({ status: 'answered', answer_turn: 'none', running })
    }
    const response = await hook.result.current.sendMessage('- Anything else?: No', undefined, answerOptions)
    assert.equal(response.answer_turn, 'none')
    assert.equal(requests.length, 1)
    assert.equal(requests[0].method, 'POST')
    assert.deepEqual(JSON.parse(requests[0].body).selected_options, { help: ['0'] })
    assert.deepEqual(hook.result.current.streamItems, [{ type: 'text', content: 'Existing answer' }])
    assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-a')
    assert.equal(hook.result.current.isStreaming, false, 'answer does not advertise a new stream')
    assert.deepEqual(await listIntents(outboxPrincipalKey(token)), [])
  })
}

test('quiet duplicate acknowledgement also does not reconnect a still-finishing publisher', async () => {
  await setup()
  let requests = 0
  globalThis.fetch = async () => {
    requests++
    return Response.json({ status: 'duplicate', answer_turn: 'none', running: true })
  }
  await hook.result.current.sendMessage('No', undefined, answerOptions)
  assert.equal(requests, 1)
  assert.equal(hook.result.current.isStreaming, false)
})

test('quiet rejection exposes server explanation and retires replay without disturbing the transcript', async () => {
  await setup()
  globalThis.fetch = async () => Response.json({ detail: 'This Goal still needs a durable next owner.' }, { status: 409 })
  await assert.rejects(hook.result.current.sendMessage('No', undefined, answerOptions), error => {
    assert.equal(error.detail, 'This Goal still needs a durable next owner.')
    assert.equal(error.status, 409)
    return true
  })
  assert.deepEqual(await listIntents(outboxPrincipalKey(token)), [])
  assert.equal(hook.result.current.streamItems[0].content, 'Existing answer')
  assert.equal(hook.result.current.isStreaming, false)
})

test('ambiguous quiet answer preserves exact option identities in the durable retry payload', async () => {
  await setup()
  globalThis.fetch = async () => Response.json({ detail: 'Temporarily unavailable' }, { status: 503 })
  await assert.rejects(hook.result.current.sendMessage('No', undefined, answerOptions), error => error.outboxRetained === true)
  const intents = await listIntents(outboxPrincipalKey(token))
  assert.equal(intents.length, 1)
  assert.equal(intents[0].type, 'answer')
  assert.equal(intents[0].body.cid, 'answer-cid')
  assert.deepEqual(intents[0].body.selected_options, { help: ['0'] })
  assert.deepEqual(intents[0].body.answers, { 'Anything else?': 'No' })
  assert.equal(hook.result.current.isStreaming, false)
})

test('quiet answer does not abort the original publishers active connection', async () => {
  await setup()
  let resolveStream
  const streamResponse = new Promise(resolve => { resolveStream = resolve })
  let streamSignal
  const requests = []
  globalThis.fetch = async (_url, options) => {
    requests.push(options.method || 'GET')
    if (options.method === 'POST') return Response.json({ status: 'answered', answer_turn: 'none', running: true })
    streamSignal = options.signal
    return streamResponse
  }
  hook.result.current.retry()
  await hook.result.current.sendMessage('No', undefined, answerOptions)
  assert.deepEqual(requests, ['GET', 'POST'])
  assert.equal(streamSignal.aborted, false)
  assert.equal(hook.result.current.isStreaming, true)
  assert.equal(hook.result.current.streamItems[0].content, 'Existing answer')
  hook.result.current.disconnect()
  resolveStream(new Response(null, { status: 204 }))
})

test('ordinary saved answers still attach to their resumed turn', async () => {
  await setup()
  const requests = []
  let resolveStream
  const streamResponse = new Promise(resolve => { resolveStream = resolve })
  globalThis.fetch = async (_url, options) => {
    requests.push(options.method || 'GET')
    if (options.method === 'POST') return Response.json({ status: 'answer_delivered', answer_turn: 'same' })
    return streamResponse
  }
  await hook.result.current.sendMessage('Yes', undefined, {
    ...answerOptions, answers: { 'Anything else?': 'Yes' }, selected_options: { help: ['1'] },
  })
  assert.deepEqual(requests, ['POST', 'GET'])
  hook.result.current.disconnect()
  resolveStream(new Response(null, { status: 204 }))
})

for (const source of ['acknowledgement', 'live-event', 'catchup-event']) {
  test(`${source} quiet settlement cannot release reading follow through unrelated publisher activity`, async () => {
    const responses = []
    await setup({ onQuestionResponseStart: key => responses.push(key) })
    let wire
    const body = new ReadableStream({ start(controller) { wire = controller } })
    globalThis.fetch = async () => new Response(body)
    const attached = hook.result.current.connectToStream(true)
    const send = event => wire.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n`))
    const settle = () => new Promise(resolve => setImmediate(resolve))
    send({ type: 'stream_snapshot', items: [
      { type: 'text', content: 'Existing answer' },
      { type: 'question', question_id: 'saved-card', questions: [{ question: 'Anything else?' }] },
    ], assistant_message_id: 'assistant-a' })
    const event = { type: 'answers_applied', question_id: 'saved-card', answers: { 'Anything else?': 'No' }, answer_turn: 'none' }
    if (source === 'catchup-event') send(event)
    send({ type: 'catch_up_done' })
    await settle()
    if (source === 'acknowledgement') {
      // A same-card response baseline might already have been armed before a
      // definitive quiet ack; only that exact baseline must be cancelled.
      hook.result.current.patchQuestionAnswers('saved-card', event.answers)
      hook.result.current.patchQuestionAnswers('saved-card', event.answers, event)
    } else if (source === 'live-event') {
      send(event)
      await settle()
    }
    send({ type: 'text_final', content: 'Publisher finished' })
    await settle()
    assert.deepEqual(responses, [])
    assert.deepEqual(hook.result.current.streamItems.find(item => item.type === 'question').answers, event.answers)
    // The quiet option must not disable another question's ordinary handoff.
    hook.result.current.patchQuestionAnswers('other-card', { 'A new question': 'Yes' })
    send({ type: 'text_final', content: 'Ordinary response' })
    await settle()
    assert.deepEqual(responses, ['question_id:other-card'])
    hook.result.current.disconnect()
    wire.close()
    await attached
  })
}


test('authoritative queued follow-up runtime attaches through the existing owner after a quiet ack', async () => {
  await setup()
  const requests = []
  let wire
  const body = new ReadableStream({ start(controller) { wire = controller } })
  globalThis.fetch = async (_url, options) => {
    requests.push(options.method || 'GET')
    if (options.method === 'POST') return Response.json({ status: 'answered', answer_turn: 'none', running: true })
    return new Response(body)
  }
  await hook.result.current.sendMessage('No', undefined, answerOptions)
  assert.deepEqual(requests, ['POST'], 'quiet acknowledgement alone never invents activity')
  // ChatView's existing active-turn poll/system-event reconciliation supplies
  // this actual runtime. Its attachment policy, not the quiet send, owns B.
  const runtime = { running: true, pendingQuestionId: null, activeAssistantMessageId: 'assistant-b' }
  assert.equal(shouldRepairRuntimeStream({
    ...runtime, isStreaming: hook.result.current.isStreaming,
    connectionError: hook.result.current.connectionError,
  }), true)
  const attached = hook.result.current.connectToStream(true)
  // Concurrent system reconciliation and polling must still share one GET.
  await hook.result.current.connectToStream(true)
  assert.deepEqual(requests, ['POST', 'GET'])
  for (const event of [
    { type: 'stream_snapshot', items: [{ type: 'text', content: 'Queued B answer' }], assistant_message_id: 'assistant-b' },
    { type: 'catch_up_done' },
  ]) wire.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n`))
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(hook.result.current.streamItems[0].content, 'Queued B answer')
  assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-b')
  hook.result.current.disconnect()
  wire.close()
  await attached
})
