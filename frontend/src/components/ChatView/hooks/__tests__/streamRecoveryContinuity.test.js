// Exercise the real stream hook at recovery boundaries without a live chat.
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import { verifyConnectivity } from '../../../../lib/connectivityStore.js'
import { listIntents, outboxPrincipalKey } from '../../chatOutbox.js'
import { renderHook } from './react-hook-shim.mjs'
import useStreamConnection from '../../useStreamConnection.js'
import { writeStoredStreamSnapshot, readStoredStreamSnapshot } from '../../streamSnapshotCache.js'

const original = Object.fromEntries(
  ['window', 'document', 'sessionStorage', 'fetch', 'localStorage', 'indexedDB'].map(key => [key, globalThis[key]]),
)
let hook

afterEach(() => {
  hook?.unmount()
  hook = null
  for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete globalThis[key]
    else globalThis[key] = value
  }
})

function deferred() {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup(callbacks = {}) {
  globalThis.window = Object.assign(new EventTarget(), { innerHeight: 800 })
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const values = new Map()
  globalThis.sessionStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  writeStoredStreamSnapshot('a', {
    items: [{ type: 'text', content: 'The answer before interruption' }],
    assistantMessageId: 'assistant-a',
  })
  globalThis.fetch = async () => new Response(null, { status: 204 })
  hook = renderHook(chat => useStreamConnection(chat, callbacks), 'a')
  return hook
}

function visibleAnswer() {
  return hook.result.current.streamItems.map(item => item.content || '').join('')
}

test('terminal 204 retains the answer until the authoritative replacement commits', async () => {
  const detail = deferred()
  const requested = deferred()
  let ended = 0
  let settled = 0
  let persistedAnswer = ''
  setup({
    onStreamEnd: () => { ended++ },
    onCatchUpSettled: () => { settled++ },
    onNeedsRefresh: async options => {
      requested.resolve(options)
      await detail.promise
      assert.equal(visibleAnswer(), 'The answer before interruption')
      // Mirrors fetchMessages' synchronous commit boundary: install durable
      // content first, then retire its live projection before yielding.
      persistedAnswer = 'The completed answer'
      options.onReconciled({ running: false })
      assert.equal(persistedAnswer + visibleAnswer(), 'The completed answer')
      return { running: false }
    },
  })
  const reconnect = hook.result.current.connectToStream(true)
  const options = await requested.promise
  assert.equal(options.authoritative, true)
  assert.equal(options.terminal204, true)
  assert.equal(visibleAnswer(), 'The answer before interruption')
  assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-a')
  assert.equal(ended, 0)
  assert.equal(settled, 0)
  assert.equal(readStoredStreamSnapshot('a').items.length, 1)
  detail.resolve()
  await reconnect
  assert.equal(visibleAnswer(), '')
  assert.equal(hook.result.current.streamAssistantMessageId, null)
  assert.equal(readStoredStreamSnapshot('a').items.length, 0)
  assert.equal(ended, 0, 'absence of broadcast must not fabricate normal turn completion')
  assert.equal(settled, 1)
  assert.equal(hook.result.current.catchUpCommitSeq, 1)
})

for (const failure of ['null', 'rejected']) {
  test(`terminal 204 with ${failure} detail retains the answer and offers existing Retry`, async () => {
    let ended = 0
    setup({
      onStreamEnd: () => { ended++ },
      onNeedsRefresh: () => failure === 'null'
        ? null
        : Promise.reject(new Error('detail unavailable')),
    })
    await hook.result.current.connectToStream(true)
    assert.equal(visibleAnswer(), 'The answer before interruption')
    assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-a')
    assert.equal(readStoredStreamSnapshot('a').items.length, 1)
    assert.equal(hook.result.current.connectionError, 'disconnected')
    assert.equal(ended, 0)
    assert.equal(hook.result.current.catchUpCommitSeq, 0)
  })
}

test('obsolete terminal detail cannot retire a successor connection or settle its catchup', async () => {
  const requested = deferred()
  const detail = deferred()
  let settled = 0
  setup({
    onCatchUpSettled: () => { settled++ },
    onNeedsRefresh: async options => {
      requested.resolve(options)
      await detail.promise
      options.onReconciled({ running: false })
    },
  })
  const reconnect = hook.result.current.connectToStream(true)
  const old = await requested.promise
  hook.result.current.disconnect()
  assert.equal(old.isCurrent(), false)
  detail.resolve()
  await reconnect
  assert.equal(visibleAnswer(), 'The answer before interruption')
  assert.equal(settled, 0)
  assert.equal(hook.result.current.catchUpCommitSeq, 0)
})

test('switching chats while terminal detail loads cannot leak or remove another chat answer', async () => {
  const requested = deferred()
  const detail = deferred()
  setup({
    onNeedsRefresh: async options => {
      requested.resolve(options)
      await detail.promise
      options.onReconciled({ running: false })
    },
  })
  writeStoredStreamSnapshot('b', {
    items: [{ type: 'text', content: 'Different chat' }],
    assistantMessageId: 'assistant-b',
  })
  const reconnect = hook.result.current.connectToStream(true)
  await requested.promise
  hook.rerender('b')
  detail.resolve()
  await reconnect
  assert.equal(visibleAnswer(), 'Different chat')
  assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-b')
})

test('manual Resume does not clear the answer while acknowledgement or catchup is pending', async () => {
  setup()
  const accepted = deferred()
  const catchup = deferred()
  const getRequested = deferred()
  globalThis.fetch = async (_url, options) => {
    if (options.method === 'POST') return accepted.promise
    getRequested.resolve()
    return catchup.promise
  }
  const resumed = hook.result.current.sendMessage('continue', undefined, { continuation: 'manual' })
  assert.equal(visibleAnswer(), 'The answer before interruption')
  assert.equal(hook.result.current.isStreaming, false, 'no optimistic Resume acknowledgement')
  accepted.resolve(Response.json({ status: 'started' }))
  await resumed
  await getRequested.promise
  assert.equal(visibleAnswer(), 'The answer before interruption')
  assert.equal(hook.result.current.isStreaming, true)
  hook.result.current.disconnect()
  catchup.resolve(new Response(null, { status: 204 }))
})

for (const [action, options] of [
  ['manual Resume', { continuation: 'manual' }],
  ['answer', { answers: { question: 'Yes' } }],
  ['force steer', { forceSteer: true }],
  ['direct steer', { directSteer: true }],
]) {
test(`rejected ${action} leaves unrelated active stream ownership intact`, async () => {
  setup()
  const stream = deferred()
  const streamRequested = deferred()
  globalThis.fetch = async (_url, options) => {
    if (options.method === 'POST') return Response.json({ detail: 'Not resumable' }, { status: 409 })
    streamRequested.resolve()
    return stream.promise
  }
  hook.result.current.retry()
  await streamRequested.promise
  assert.equal(hook.result.current.isStreaming, true)
  await assert.rejects(
    hook.result.current.sendMessage('continue', undefined, options),
  )
  assert.equal(hook.result.current.isStreaming, true)
  assert.equal(visibleAnswer(), 'The answer before interruption')
  hook.result.current.disconnect()
  stream.resolve(new Response(null, { status: 204 }))
})
}

test('failed terminal detail can retry into an atomic authoritative replacement', async () => {
  let fail = true
  setup({
    onNeedsRefresh: options => {
      if (fail) return null
      options.onReconciled({ running: false })
      return { running: false }
    },
  })
  await hook.result.current.connectToStream(true)
  assert.equal(visibleAnswer(), 'The answer before interruption')
  assert.equal(hook.result.current.connectionError, 'disconnected')
  fail = false
  await hook.result.current.connectToStream(true)
  assert.equal(visibleAnswer(), '')
  assert.equal(hook.result.current.connectionError, null)
  assert.equal(hook.result.current.isStreaming, false)
})

test('manual Resume waits for complete server catchup before replacing the prior answer', async () => {
  const settled = deferred()
  setup({ onCatchUpSettled: () => settled.resolve() })
  let wire
  const stream = new ReadableStream({ start(controller) { wire = controller } })
  globalThis.fetch = async (_url, options) => {
    if (options.method !== 'POST') return new Response(stream)
    assert.equal(JSON.parse(options.body).resume_run_id, 'interrupted-run')
    return Response.json({ status: 'started' })
  }
  await hook.result.current.sendMessage('continue', undefined, {
    continuation: 'manual', resumeRunId: 'interrupted-run',
  })
  assert.equal(visibleAnswer(), 'The answer before interruption')
  const encoder = new TextEncoder()
  wire.enqueue(encoder.encode('data: ' + JSON.stringify({
    type: 'stream_snapshot', items: [{ type: 'text', content: 'Resumed answer' }],
    assistant_message_id: 'assistant-resumed',
  }) + '\n'))
  // Give the actual reader its snapshot; only catch_up_done can publish it.
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(visibleAnswer(), 'The answer before interruption')
  wire.enqueue(encoder.encode('data: {"type":"catch_up_done"}\n'))
  await settled.promise
  assert.equal(visibleAnswer(), 'Resumed answer')
  assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-resumed')
  hook.result.current.disconnect()
  wire.close()
})

test('late read failure from a replaced stream cannot mark the new connection disconnected', async () => {
  setup()
  const read = deferred()
  const reading = deferred()
  globalThis.fetch = async () => ({
    status: 200, ok: true,
    body: { getReader: () => ({ read: () => { reading.resolve(); return read.promise } }) },
  })
  const oldConnection = hook.result.current.connectToStream(true)
  await reading.promise
  hook.result.current.disconnect()
  read.reject(new Error('old socket failed after its replacement'))
  await oldConnection
  assert.equal(hook.result.current.connectionError, null)
  assert.equal(visibleAnswer(), 'The answer before interruption')
})

for (const [action, sendOptions] of [
  ['Resume', { continuation: 'manual', resumeRunId: 'interrupted-a' }],
  ['queued follow-up', { queueOnly: true }],
  ['ordinary send', {}],
]) {
  test(`late ${action} acknowledgement cannot attach another chat`, async () => {
    setup()
    const accepted = deferred()
    const posted = deferred()
    const calls = []
    globalThis.fetch = async (url, options) => {
      calls.push(url)
      assert.equal(options.method, 'POST', 'obsolete acknowledgement must not attach a stream')
      posted.resolve()
      return accepted.promise
    }
    const sending = hook.result.current.sendMessage('message for A', undefined, sendOptions)
    await posted.promise
    writeStoredStreamSnapshot('b', {
      items: [{ type: 'text', content: 'Another chat answer' }],
      assistantMessageId: 'assistant-b',
    })
    hook.rerender('b')
    accepted.resolve(Response.json({ status: 'started' }))
    await sending
    assert.deepEqual(calls, ['/api/chats/a/messages'])
    assert.equal(visibleAnswer(), 'Another chat answer')
    assert.equal(hook.result.current.streamAssistantMessageId, 'assistant-b')
  })
}

test('unmount during Resume acknowledgement does not reconnect a dead view', async () => {
  setup()
  const accepted = deferred()
  const posted = deferred()
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    assert.equal(options.method, 'POST')
    posted.resolve()
    return accepted.promise
  }
  const sending = hook.result.current.sendMessage('continue', undefined, {
    continuation: 'manual', resumeRunId: 'interrupted-a',
  })
  await posted.promise
  hook.unmount()
  accepted.resolve(Response.json({ status: 'started' }))
  await sending
  assert.deepEqual(calls, ['/api/chats/a/messages'])
})


test('chat switches during durable intent registration keep POST and retirement bound to the original chat', async () => {
  setup()
  globalThis.indexedDB = new IDBFactory()
  const token = `stub.${Buffer.from(JSON.stringify({ sub: 'test-owner', epoch: 1 })).toString('base64url')}.stub`
  globalThis.localStorage = { getItem: () => token }
  const accepted = deferred()
  const posted = deferred()
  let path
  globalThis.fetch = async (url, options) => {
    path = url
    assert.equal(options.method, 'POST')
    posted.resolve()
    return accepted.promise
  }
  const sending = hook.result.current.sendMessage('continue', undefined, {
    cid: 'resume-original-chat', continuation: 'manual', resumeRunId: 'interrupted-a',
  })
  // enqueueIntent has not yielded back yet: the view switches before the
  // durable write finishes and before HTTP begins.
  hook.rerender('b')
  await posted.promise
  const principal = outboxPrincipalKey(token)
  const pending = await listIntents(principal)
  assert.equal(path, '/api/chats/a/messages')
  assert.equal(pending.length, 1)
  assert.equal(pending[0].chatId, 'a')
  assert.equal(pending[0].body.resume_run_id, 'interrupted-a')
  accepted.resolve(Response.json({ status: 'started' }))
  await sending
  assert.deepEqual(await listIntents(principal), [], 'accepted intent retires in its original chat')
})

for (const order of ['visible-online', 'online-visible']) {
  test(`wake ${order} and runtime attachment share a pending replacement`, async () => {
    setup()
    const requests = []
    const firstRead = deferred()
    globalThis.fetch = async (url, options) => {
      requests.push(options.signal)
      if (requests.length > 1) return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(
          Object.assign(new Error('aborted'), { name: 'AbortError' }),
        ), { once: true })
      })
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"catch_up_done"}\n\n'))
        options.signal.addEventListener('abort', () => controller.close(), { once: true })
        firstRead.resolve()
      } }), { status: 200 })
    }
    void hook.result.current.connectToStream(true)
    await firstRead.promise
    await Promise.resolve()
    const originalNow = Date.now
    try {
      let now = originalNow()
      Date.now = () => now
      document.visibilityState = 'hidden'
      document.dispatchEvent(new Event('visibilitychange'))
      window.dispatchEvent(new Event('offline'))
      now += 16000
      const visible = () => {
        document.visibilityState = 'visible'
        document.dispatchEvent(new Event('visibilitychange'))
      }
      const online = () => window.dispatchEvent(new Event('online'))
      if (order === 'visible-online') { visible(); online() }
      else { online(); visible() }
      void hook.result.current.connectToStream(true)
      assert.equal(requests.length, 2, 'one old stream and one replacement, not a third GET')
      assert.equal(requests[0].aborted, true)
      assert.equal(requests[1].aborted, false, 'wake signals must not abort the new GET')
      hook.result.current.disconnect({ clearStreaming: true })
      window.dispatchEvent(new Event('online'))
      assert.equal(requests.length, 2, 'Stop must retain ownership over later wake signals')
    } finally { Date.now = originalNow }
  })
}

test('repeated attachment shares a live stream but explicit Retry replaces it', async () => {
  setup()
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push(options.signal)
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"catch_up_done"}\n\n'))
      options.signal.addEventListener('abort', () => controller.close(), { once: true })
    } }), { status: 200 })
  }
  void hook.result.current.connectToStream(true)
  await Promise.resolve()
  void hook.result.current.connectToStream(true)
  assert.equal(requests.length, 1)
  hook.result.current.retry()
  assert.equal(requests.length, 2)
  assert.equal(requests[0].aborted, true)
})

test('a shared pending GET still has a no-read deadline, cancelled by unmount', async () => {
  setup()
  window.__MOBIUS_KEPT_SOCKET_DEADMAN_MS = 10
  const replacement = deferred()
  const requests = []
  globalThis.fetch = (url, options) => {
    requests.push(options.signal)
    if (requests.length === 2) replacement.resolve()
    return new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(
        Object.assign(new Error('aborted'), { name: 'AbortError' }),
      ), { once: true })
    })
  }
  void hook.result.current.connectToStream(true)
  void hook.result.current.connectToStream(true)
  assert.equal(requests.length, 1)
  await replacement.promise
  assert.equal(requests.length, 2)
  assert.equal(requests[0].aborted, true)
  hook.unmount()
  hook = null
  assert.equal(requests[1].aborted, true)
})


test('a successful attachment announcing network recovery must not cancel itself', async () => {
  setup()
  const requests = []
  const checked = verifyConnectivity()
  globalThis.fetch = async (url, options) => {
    requests.push(options.signal)
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"catch_up_done"}\n\n'))
      options.signal.addEventListener('abort', () => controller.close(), { once: true })
    } }), { status: 200 })
  }
  void hook.result.current.connectToStream(true)
  await checked
  await Promise.resolve()
  assert.equal(requests.length, 1)
  assert.equal(requests[0].aborted, false)
})

test('a redundant online signal cannot show Reconnecting after catchup settled', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const settled = deferred()
  setup({ onCatchUpSettled: () => settled.resolve() })
  let requests = 0
  globalThis.fetch = async (url, options) => {
    requests++
    return new Response(new ReadableStream({ start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"catch_up_done"}\n\n'))
      options.signal.addEventListener('abort', () => controller.close(), { once: true })
    } }), { status: 200 })
  }
  void hook.result.current.connectToStream(true)
  await settled.promise
  window.dispatchEvent(new Event('online'))
  t.mock.timers.tick(1600)
  assert.equal(requests, 1)
  assert.equal(hook.result.current.reconnecting, false)
})
