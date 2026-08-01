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
  reclaimStoredStreamSnapshots,
  flushStoredStreamSnapshot,
  _resetStreamSnapshotBufferForTests,
} from '../streamSnapshotCache.js'

function makeStorage() {
  const map = new Map()
  return {
    map,
    get length() { return map.size },
    key(index) { return [...map.keys()][index] ?? null },
    getItem(key) { return map.has(key) ? map.get(key) : null },
    setItem(key, value) { map.set(key, value) },
    removeItem(key) { map.delete(key) },
  }
}

// A storage that counts setItem/removeItem so the write-behind tests can assert
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
  flushStoredStreamSnapshot('chat-a')

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), true)
})

test('stream snapshot ignores empty writes so reconnect reset keeps visible cache', () => {
  const storage = makeStorage()
  const items = [{ type: 'tool', tool: 'Bash', status: 'running' }]

  writeStoredStreamSnapshot('chat-a', items, storage)
  writeStoredStreamSnapshot('chat-a', [], storage)
  flushStoredStreamSnapshot('chat-a')

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
})

test('clear removes the current key', () => {
  const storage = makeStorage()
  storage.setItem(streamSnapshotKey('chat-a'), JSON.stringify([{ type: 'text', content: 'new' }]))

  clearStoredStreamSnapshot('chat-a', storage)

  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), false)
})

test('quota reclamation drops only stream cache, including pending writes', () => {
  _resetStreamSnapshotBufferForTests()
  const storage = makeStorage()
  storage.setItem('draft:chat-a', 'owner data')
  storage.setItem(streamSnapshotKey('settled'), '[{"type":"text"}]')
  writeStoredStreamSnapshot('pending', [{ type: 'text', content: 'later' }], storage)

  assert.equal(reclaimStoredStreamSnapshots(storage), 1)
  assert.equal(storage.getItem('draft:chat-a'), 'owner data')
  assert.equal(storage.getItem(streamSnapshotKey('settled')), null)
  assert.equal(storage.getItem(streamSnapshotKey('pending')), null)
  _resetStreamSnapshotBufferForTests()
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

// ── Write-behind + lossless flush contract ──────────────────────────────────
// The snapshot is the remount/reconnect fallback, so buffering without a flush
// would reintroduce partial-text rollback. These lock in latest-wins buffering
// for every chat and a synchronous flush at every lifecycle boundary.

test('single-chat rapid writes stay buffered instead of blocking each reveal frame', () => {
  _resetStreamSnapshotBufferForTests()
  const s = makeSpyStorage()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'a' }], s)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'ab' }], s)
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'abc' }], s)
  // Rapid writes stay in memory — nothing serialized yet.
  assert.equal(s.calls.set, 0)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [])
  // The flush lands exactly one write carrying the LATEST items (lossless).
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [{ type: 'text', content: 'abc' }])
  _resetStreamSnapshotBufferForTests()
})

test('a flush boundary writes synchronously and is idempotent', () => {
  _resetStreamSnapshotBufferForTests()
  const s = makeSpyStorage()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'x' }], s)
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  // No pending write remains — a second flush at a later boundary is a no-op.
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 1)
  _resetStreamSnapshotBufferForTests()
})

test('clear drops a pending buffered write so it cannot resurrect', () => {
  _resetStreamSnapshotBufferForTests()
  const s = makeSpyStorage()
  writeStoredStreamSnapshot('c1', [{ type: 'text', content: 'stale' }], s)
  clearStoredStreamSnapshot('c1', s)
  assert.equal(s.calls.remove, 1)
  // A later lifecycle flush cannot resurrect the dropped write.
  flushStoredStreamSnapshot('c1')
  assert.equal(s.calls.set, 0)
  assert.deepEqual(readStoredStreamSnapshot('c1', s), [])
  _resetStreamSnapshotBufferForTests()
})
