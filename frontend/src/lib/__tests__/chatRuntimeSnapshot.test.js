import assert from 'node:assert/strict'
import { describe, it, mock } from 'node:test'
import {
  invalidateChatRuntimeSnapshot,
  readChatRuntimeSnapshot,
} from '../chatRuntimeSnapshot.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve
    reject = onReject
  })
  return { promise, resolve, reject }
}

describe('chat runtime snapshot reads', () => {
  it('shares one in-flight read while preserving the payload for every view', async () => {
    const gate = deferred()
    const read = mock.fn(() => gate.promise)

    const first = readChatRuntimeSnapshot('owner-a', 'same-chat', read)
    const second = readChatRuntimeSnapshot('owner-a', 'same-chat', read)
    gate.resolve({ runtime_revision: 7, running: true })

    assert.deepEqual(await first, { runtime_revision: 7, running: true })
    assert.deepEqual(await second, { runtime_revision: 7, running: true })
    assert.equal(read.mock.callCount(), 1)
  })

  it('releases a failed owner so a later recovery read can run', async () => {
    const failed = mock.fn(() => Promise.reject(new Error('offline')))
    await assert.rejects(
      readChatRuntimeSnapshot('owner-b', 'retry-chat', failed),
      /offline/,
    )

    const recovered = mock.fn(() => Promise.resolve({ runtime_revision: 8 }))
    assert.deepEqual(
      await readChatRuntimeSnapshot('owner-b', 'retry-chat', recovered),
      { runtime_revision: 8 },
    )
    assert.equal(recovered.mock.callCount(), 1)
  })

  it('does not couple independent chats', async () => {
    const firstRead = mock.fn(() => Promise.resolve({ runtime_revision: 1 }))
    const secondRead = mock.fn(() => Promise.resolve({ runtime_revision: 2 }))

    assert.deepEqual(await Promise.all([
      readChatRuntimeSnapshot('owner-c', 'chat-a', firstRead),
      readChatRuntimeSnapshot('owner-c', 'chat-b', secondRead),
    ]), [
      { runtime_revision: 1 },
      { runtime_revision: 2 },
    ])
  })

  it('does not share a chat across authenticated principals', async () => {
    const firstRead = mock.fn(() => Promise.resolve({ owner: 'a' }))
    const secondRead = mock.fn(() => Promise.resolve({ owner: 'b' }))

    assert.deepEqual(await Promise.all([
      readChatRuntimeSnapshot('owner-d', 'shared-id', firstRead),
      readChatRuntimeSnapshot('owner-e', 'shared-id', secondRead),
    ]), [{ owner: 'a' }, { owner: 'b' }])
    assert.equal(firstRead.mock.callCount(), 1)
    assert.equal(secondRead.mock.callCount(), 1)
  })

  it('moves old consumers onto a fresh successor after invalidation', async () => {
    const stale = deferred()
    const fresh = deferred()
    let reads = 0
    const read = mock.fn(() => {
      reads += 1
      return reads === 1 ? stale.promise : fresh.promise
    })

    const oldConsumer = readChatRuntimeSnapshot('owner-f', 'mutating-chat', read)
    await Promise.resolve()
    invalidateChatRuntimeSnapshot('owner-f', 'mutating-chat')
    const newConsumer = readChatRuntimeSnapshot('owner-f', 'mutating-chat', read)
    stale.resolve({ runtime_revision: 1, running: false })
    fresh.resolve({ runtime_revision: 2, running: true })

    assert.deepEqual(await oldConsumer, { runtime_revision: 2, running: true })
    assert.deepEqual(await newConsumer, { runtime_revision: 2, running: true })
    assert.equal(read.mock.callCount(), 2)
  })

  it('an obsolete late completion joins and cannot clear a newer owner', async () => {
    const obsolete = deferred()
    const firstSuccessor = deferred()
    const current = deferred()
    const reads = [obsolete, firstSuccessor, current]
    let readIndex = 0
    const read = mock.fn(() => {
      const next = reads[readIndex]
      readIndex += 1
      return next.promise
    })

    const oldConsumer = readChatRuntimeSnapshot('owner-g', 'late-chat', read)
    await Promise.resolve()
    invalidateChatRuntimeSnapshot('owner-g', 'late-chat')
    const firstNewConsumer = readChatRuntimeSnapshot('owner-g', 'late-chat', read)
    firstSuccessor.resolve({ runtime_revision: 2 })
    assert.deepEqual(await firstNewConsumer, { runtime_revision: 2 })

    const currentConsumer = readChatRuntimeSnapshot('owner-g', 'late-chat', read)
    obsolete.resolve({ runtime_revision: 1 })
    await Promise.resolve()
    assert.equal(read.mock.callCount(), 3)

    current.resolve({ runtime_revision: 3 })
    assert.deepEqual(await oldConsumer, { runtime_revision: 3 })
    assert.deepEqual(await currentConsumer, { runtime_revision: 3 })
  })
})
