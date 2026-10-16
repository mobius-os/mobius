import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import {
  forgetChatFirstPageRead,
  readChatFirstPage,
  resetChatFirstPageReadsForTests,
} from '../chatDetailReads.js'

const response = (status, body) => ({
  status, ok: status >= 200 && status < 300, json: async () => body,
})
function deferredRequest() {
  const releases = []
  const calls = []
  const signals = []
  const request = signal => {
    calls.push(1)
    signals.push(signal)
    return new Promise((resolve, reject) => {
      releases.push(resolve)
      signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
    })
  }
  // The shared read starts its request on the next microtask.
  const release = async (value, index = 0) => {
    while (!releases[index]) await Promise.resolve()
    releases[index](value)
  }
  return { request, calls, signals, release }
}

beforeEach(resetChatFirstPageReadsForTests)

test('a warming read and the activation it precedes share one request', async () => {
  const pending = deferredRequest()
  const warming = readChatFirstPage('c1', pending.request)
  const activation = readChatFirstPage('c1', pending.request)
  await pending.release(response(200, { messages: [] }))
  assert.deepEqual(await warming, await activation)
  assert.equal(pending.calls.length, 1)
  assert.deepEqual(await activation, { status: 200, ok: true, data: { messages: [] } })
})

test('a settled read is never reused and other chats never join', async () => {
  let calls = 0
  const request = async () => { calls += 1; return response(200, { n: calls }) }
  assert.deepEqual((await readChatFirstPage('c1', request)).data, { n: 1 })
  assert.deepEqual((await readChatFirstPage('c1', request)).data, { n: 2 })
  await Promise.all([readChatFirstPage('c1', request), readChatFirstPage('c2', request)])
  assert.equal(calls, 4)
})

test('each caller reads the status itself, so a 404 stays a 404', async () => {
  const read = await readChatFirstPage('gone', async () => response(404, { detail: 'Not found' }))
  assert.deepEqual(read, { status: 404, ok: false, data: null })
})

test('one caller giving up does not fail the shared read for the other', async () => {
  const pending = deferredRequest()
  const controller = new AbortController()
  const leaving = readChatFirstPage('c1', pending.request, { signal: controller.signal })
  const staying = readChatFirstPage('c1', pending.request, { signal: new AbortController().signal })
  controller.abort(new Error('unmounted'))
  await assert.rejects(leaving, /unmounted/)
  await pending.release(response(200, { ok: 1 }))
  assert.deepEqual((await staying).data, { ok: 1 })
  assert.equal(pending.signals[0].aborted, false, 'a remaining waiter keeps the request')
})

test('the shared request is aborted when its last waiter leaves', async () => {
  const pending = deferredRequest()
  const warming = new AbortController()
  const activation = new AbortController()
  const first = readChatFirstPage('c1', pending.request, { signal: warming.signal })
  const second = readChatFirstPage('c1', pending.request, { signal: activation.signal })
  while (!pending.signals[0]) await Promise.resolve()
  warming.abort(new Error('drawer closed'))
  await assert.rejects(first, /drawer closed/)
  assert.equal(pending.signals[0].aborted, false)
  activation.abort(new Error('unmounted'))
  await assert.rejects(second, /unmounted/)
  assert.equal(pending.signals[0].aborted, true, 'nobody is waiting, so the connection is released')
  // A later open starts a fresh read rather than joining the cancelled one.
  const later = readChatFirstPage('c1', pending.request)
  await pending.release(response(200, { n: 'fresh' }), 1)
  assert.equal(pending.calls.length, 2)
  assert.deepEqual((await later).data, { n: 'fresh' })
})

test('a caller whose signal is already aborted neither joins nor starts a read', async () => {
  const pending = deferredRequest()
  const controller = new AbortController()
  controller.abort(new Error('gone'))
  await assert.rejects(readChatFirstPage('c1', pending.request, { signal: controller.signal }), /gone/)
  assert.equal(pending.calls.length, 0)
})

test('a read begun before a run signal is not joined after it', async () => {
  const pending = deferredRequest()
  const warming = readChatFirstPage('c1', pending.request)
  forgetChatFirstPageRead('c1')
  const activation = readChatFirstPage('c1', pending.request)
  assert.notEqual(activation, warming)
  // The superseded read settles while the newer one is still in flight.
  await pending.release(response(200, { n: 'before' }), 0)
  assert.deepEqual((await warming).data, { n: 'before' })
  // Finishing late must not evict the newer one's entry: a third caller joins it.
  const joining = readChatFirstPage('c1', pending.request)
  await pending.release(response(200, { n: 'after' }), 1)
  assert.equal(pending.calls.length, 2)
  assert.deepEqual((await activation).data, { n: 'after' })
  assert.deepEqual((await joining).data, { n: 'after' })
})
