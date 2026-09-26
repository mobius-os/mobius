import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { resetRuntimeReadsForTests, sharedRuntimeRead } from '../runtimeReads.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

beforeEach(() => resetRuntimeReadsForTests())

test('views of the same chat share one in-flight read', async () => {
  const response = deferred()
  let reads = 0
  const read = () => { reads += 1; return response.promise }

  const visible = sharedRuntimeRead('chat-a', read)
  const retained = sharedRuntimeRead('chat-a', read)
  assert.equal(reads, 1)
  assert.equal(visible, retained)

  response.resolve({ runtime_revision: 3 })
  assert.deepEqual(await retained, { runtime_revision: 3 })
})

test('different chats read independently', () => {
  let reads = 0
  const read = () => { reads += 1; return new Promise(() => {}) }
  sharedRuntimeRead('chat-a', read)
  sharedRuntimeRead('chat-b', read)
  assert.equal(reads, 2)
})

test('a settled read releases the chat for the next read', async () => {
  let reads = 0
  await sharedRuntimeRead('chat-a', async () => { reads += 1; return { runtime_revision: 1 } })
  await sharedRuntimeRead('chat-a', async () => { reads += 1; return { runtime_revision: 2 } })
  assert.equal(reads, 2)
})

test('a failed read is shared and then released', async () => {
  const response = deferred()
  const first = sharedRuntimeRead('chat-a', () => response.promise)
  const second = sharedRuntimeRead('chat-a', () => Promise.resolve('unused'))
  response.reject(new Error('Runtime refresh failed'))
  await assert.rejects(first, /Runtime refresh failed/)
  await assert.rejects(second, /Runtime refresh failed/)
  assert.equal(await sharedRuntimeRead('chat-a', async () => 'fresh'), 'fresh')
})
