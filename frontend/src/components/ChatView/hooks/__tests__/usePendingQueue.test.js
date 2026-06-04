/**
 * Unit tests for usePendingQueue.
 *
 * Run with:
 *   cd frontend && node --loader=./src/components/ChatView/hooks/__tests__/react-loader.mjs \
 *     --test src/components/ChatView/hooks/__tests__/usePendingQueue.test.js
 *
 * The loader aliases `react` -> react-hook-shim so the hook can be
 * driven from node without a renderer. See react-hook-shim.mjs.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import usePendingQueue from '../usePendingQueue.js'

function fixtureMsg(overrides = {}) {
  return {
    role: 'user',
    content: 'hi',
    ts: 100,
    cid: 'cid-1',
    queued: true,
    ...overrides,
  }
}

test('add appends and the ref updates synchronously', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg())
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'cid-1')
  assert.equal(result.current.pendingMessagesRef.current[0].position, 1)
  result.current.add(fixtureMsg({ cid: 'cid-2', ts: 200 }))
  assert.equal(result.current.pendingMessagesRef.current.length, 2)
  assert.equal(result.current.pendingMessagesRef.current[1].position, 2)
})

test('swapOptimisticTs preserves cid while updating ts and position', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  result.current.swapOptimisticTs('optimistic-x', 12345, 3)
  const list = result.current.pendingMessagesRef.current
  assert.equal(list[0].cid, 'optimistic-x', 'cid must persist across ts swap')
  assert.equal(list[0].ts, 12345)
  assert.equal(list[0].position, 3)
})

test('promoteByTs returns the matching entry and removes it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2 }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3 }))
  const got = result.current.promoteByTs(2)
  assert.equal(got.cid, 'b')
  assert.equal(result.current.pendingMessagesRef.current.length, 2)
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a', 'c'],
  )
})

test('promoteByTs returns null when no match', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  assert.equal(result.current.promoteByTs(99), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('promoteAll collapses the matching entry and everything after it', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteAll(2)
  assert.equal(got.cid, 'b')
  assert.equal(got.ts, 2)
  assert.equal(got.content, 'second\nthird')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['a'],
  )
})

test('promoteAll only consumes the queue present at promotion time', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  const got = result.current.promoteAll(1)
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  assert.equal(got.content, 'first\nsecond')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['c'],
  )
})

test('promoteManyByTs preserves later entries not consumed by the backend', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1, content: 'first' }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2, content: 'second' }))
  result.current.add(fixtureMsg({ cid: 'c', ts: 3, content: 'third' }))
  const got = result.current.promoteManyByTs([1, 2])
  assert.equal(got.content, 'first\nsecond')
  assert.deepEqual(
    result.current.pendingMessagesRef.current.map(m => m.cid),
    ['c'],
  )
})

test('swapOptimisticTs removes a chip whose server ts was already consumed', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-a', ts: 999, content: 'first' }))
  assert.equal(result.current.promoteManyByTs([10]), null)
  result.current.swapOptimisticTs('optimistic-a', 10, 1)
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})

test('promoteAll returns null when no matching anchor exists', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  assert.equal(result.current.promoteAll(99), null)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
})

test('cancelByTs removes by ts', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'a', ts: 1 }))
  result.current.add(fixtureMsg({ cid: 'b', ts: 2 }))
  result.current.cancelByTs(1)
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'b')
})

test('cancelByCid removes the matching entry (pre-swap rollback path)', () => {
  // Used in doSend's error rollback and the "server said started"
  // branch — both run before the optimistic ts has been swapped to
  // the server ts, so cid is the only stable handle.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 99 }))
  result.current.add(fixtureMsg({ cid: 'optimistic-y', ts: 100 }))
  result.current.cancelByCid('optimistic-x')
  assert.equal(result.current.pendingMessagesRef.current.length, 1)
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'optimistic-y')
})

test('hydrate replaces wholesale; ref updates synchronously', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'local', ts: 1 }))
  result.current.hydrate([
    { role: 'user', content: 'srv', ts: 50 },
    { role: 'user', content: 'srv2', ts: 60 },
  ])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 2)
  assert.equal(list[0].cid, 's-50')
  assert.equal(list[1].cid, 's-60')
  assert.equal(list[0].queued, true)
})

test('hydrate preserves the local cid when server ts matches an existing entry', () => {
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  result.current.swapOptimisticTs('optimistic-x', 12345)
  result.current.hydrate([{ role: 'user', content: 'hi', ts: 12345 }])
  const list = result.current.pendingMessagesRef.current
  assert.equal(list.length, 1)
  assert.equal(list[0].cid, 'optimistic-x',
    'hydrate must reuse the local cid when ts matches')
})

test('swapOptimisticTs racing with hydrate keeps cid stability', () => {
  // R2 from _034-design.md: a hydrate (slow refetch) landing
  // interleaved with an optimistic add+swap must not flip the cid
  // out from under QueuedMessages. Sequence: add optimistic, swap
  // its ts to the value the server is about to claim, then a
  // hydrate carrying that server ts arrives — cid must stay
  // 'optimistic-x', not become 's-12345'.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ cid: 'optimistic-x', ts: 999 }))
  // Server replied with the canonical ts; we swapped locally.
  result.current.swapOptimisticTs('optimistic-x', 12345)
  // Concurrent hydrate from a refetch that started before our swap
  // landed but resolved after it — server's view also has ts=12345.
  result.current.hydrate([{ role: 'user', content: 'hi', ts: 12345 }])
  assert.equal(result.current.pendingMessagesRef.current[0].cid, 'optimistic-x')
})

test('clear resets state and ref synchronously', () => {
  // R1 from _034-design.md: handleStop calls clear() then reads
  // pendingMessagesRef.current synchronously to decide whether to
  // skip an in-flight fetchMessages. If clear only scheduled a
  // render, the ref read would still see the pre-stop queue.
  const { result } = renderHook(usePendingQueue)
  result.current.add(fixtureMsg({ ts: 1 }))
  result.current.add(fixtureMsg({ ts: 2 }))
  result.current.clear()
  assert.deepEqual(result.current.pendingMessagesRef.current, [])
})
