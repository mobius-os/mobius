/*
 * Tests for versioned live-stream sessionStorage cache helpers.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  streamSnapshotKey,
  readStoredStreamSnapshot,
  writeStoredStreamSnapshot,
  clearStoredStreamSnapshot,
  flushStoredStreamSnapshot,
  flushAllStreamSnapshots,
  registerMountedChat,
  unregisterMountedChat,
  getMountedChatCount,
  _resetStreamSnapshotThrottleForTests,
  STREAM_SNAPSHOT_THROTTLE_MS,
} from '../streamSnapshotCache.js'

function makeStorage() {
  const map = new Map()
  return {
    map,
    getItem(key) { return map.has(key) ? map.get(key) : null },
    setItem(key, value) { map.set(key, value) },
    removeItem(key) { map.delete(key) },
  }
}

// A storage that counts setItem/removeItem so the throttle tests can assert
// "nothing serialized yet" vs "exactly one write" instead of only end-state.
function makeSpyStorage() {
  const map = new Map()
  const calls = { set: 0, remove: 0 }
  return {
    map,
    calls,
    getItem(key) { return map.has(key) ? map.get(key) : null },
    setItem(key, value) { calls.set += 1; map.set(key, value) },
    removeItem(key) { calls.remove += 1; map.delete(key) },
  }
}

test('stream snapshot read/write uses v2 key', () => {
  const storage = makeStorage()
  const items = [{ type: 'text', content: 'partial' }]

  writeStoredStreamSnapshot('chat-a', items, storage)

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), true)
})

test('stream snapshot ignores empty writes so reconnect reset keeps visible cache', () => {
  const storage = makeStorage()
  const items = [{ type: 'tool', tool: 'Bash', status: 'running' }]

  writeStoredStreamSnapshot('chat-a', items, storage)
  writeStoredStreamSnapshot('chat-a', [], storage)

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
})

test('clear removes the current key', () => {
  const storage = makeStorage()
  storage.setItem(streamSnapshotKey('chat-a'), JSON.stringify([{ type: 'text', content: 'new' }]))

  clearStoredStreamSnapshot('chat-a', storage)

  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), false)
})

test('read returns [] for corrupt or absent values', () => {
  const storage = makeStorage()
  storage.setItem(streamSnapshotKey('bad'), '{nope')

  assert.deepEqual(readStoredStreamSnapshot('missing', storage), [])
  assert.deepEqual(readStoredStreamSnapshot('bad', storage), [])
})

test('default cache is optional when an opaque sandbox denies sessionStorage', () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    get() { throw new DOMException('Blocked by opaque sandbox', 'SecurityError') },
  })
  try {
    assert.deepEqual(readStoredStreamSnapshot('chat-a'), [])
    assert.doesNotThrow(() => writeStoredStreamSnapshot('chat-a', [{ type: 'text' }]))
    assert.doesNotThrow(() => clearStoredStreamSnapshot('chat-a'))
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'sessionStorage', descriptor)
    else delete globalThis.sessionStorage
  }
})

// ── Multi-pane throttle + lossless flush contract (design §2 perf budget) ────
// The snapshot is the remount/reconnect fallback, so a throttle without a flush
// would reintroduce partial-text rollback. These lock in: unthrottled single
// mount, trailing coalescing while >1 mounted, and a synchronous flush at every
// boundary.

test('single-mount writes stay synchronous and unthrottled (byte-identical)', () => {
  _resetStreamSnapshotThrottleForTests()
  const s = makeSpyStorage()
  registerMountedChat() // count 1 → still <=1, no throttle
  const items = [{ type: 'text', content: 'hi' }]
  writeStoredStreamSnapshot('c1', items, s)
  assert.equal(s.calls.set, 1)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), items)
  _resetStreamSnapshotThrottleForTests()
})

test('multi-pane rapid writes coalesce to one lossless trailing flush', () => {
  _resetStreamSnapshotThrottleForTests()
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat() // count 2 → throttled
  assert.equal(getMountedChatCount(), 2)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'a' }], s)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'ab' }], s)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'abc' }], s)
  // Rapid writes are coalesced — nothing serialized yet.
  assert.equal(s.calls.set, 0)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [])
  // The flush lands exactly one write carrying the LATEST items (lossless).
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [{ type: 'text', content: 'abc' }])
  _resetStreamSnapshotThrottleForTests()
})

test('a flush boundary writes synchronously and is idempotent', () => {
  _resetStreamSnapshotThrottleForTests()
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'x' }], s)
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  // No pending write remains — a second flush at a later boundary is a no-op.
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  _resetStreamSnapshotThrottleForTests()
})

test('flushAllStreamSnapshots lands every pending chat (page-global boundary)', () => {
  _resetStreamSnapshotThrottleForTests()
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: '1' }], s)
  writeStoredStreamSnapshot('c2', [{ type: 'text', content: '2' }], s)
  assert.equal(s.calls.set, 0)
  flushAllStreamSnapshots()
  assert.equal(s.calls.set, 2)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [{ type: 'text', content: '1' }])
  assert.deepEqual(readStoredStreamSnapshot('c2', s), [{ type: 'text', content: '2' }])
  _resetStreamSnapshotThrottleForTests()
})

test('the trailing timer lands the coalesced write after the window', (t) => {
  _resetStreamSnapshotThrottleForTests()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'p' }], s)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'pq' }], s)
  assert.equal(s.calls.set, 0)
  t.mock.timers.tick(STREAM_SNAPSHOT_THROTTLE_MS)
  assert.equal(s.calls.set, 1)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [{ type: 'text', content: 'pq' }])
  _resetStreamSnapshotThrottleForTests()
})

test('clear drops a pending coalesced write so it cannot resurrect', (t) => {
  _resetStreamSnapshotThrottleForTests()
  t.mock.timers.enable({ apis: ['setTimeout'] })
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'stale' }], s)
  clearStoredStreamSnapshot('c1', s)
  assert.equal(s.calls.remove, 1)
  // Even after the throttle window elapses, the dropped write never lands.
  t.mock.timers.tick(STREAM_SNAPSHOT_THROTTLE_MS + 10)
  assert.equal(s.calls.set, 0)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [])
  _resetStreamSnapshotThrottleForTests()
})

test('falling back to a single mount writes synchronously and drops stale pending', () => {
  _resetStreamSnapshotThrottleForTests()
  const s = makeSpyStorage()
  registerMountedChat(); registerMountedChat() // 2 → throttled
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'old' }], s)
  assert.equal(s.calls.set, 0) // coalesced, not yet written
  unregisterMountedChat() // back to 1 → unthrottled
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'new' }], s)
  // The synchronous write lands once and the stale pending 'old' is dropped, so
  // it can never clobber the fresher value afterward.
  assert.equal(s.calls.set, 1)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [{ type: 'text', content: 'new' }])
  _resetStreamSnapshotThrottleForTests()
})
