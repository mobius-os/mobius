/*
 * Tests for versioned live-stream sessionStorage cache helpers.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  streamSnapshotKey,
  legacyStreamSnapshotKey,
  readStoredStreamSnapshot,
  writeStoredStreamSnapshot,
  clearStoredStreamSnapshot,
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

test('stream snapshot read/write uses v2 key', () => {
  const storage = makeStorage()
  const items = [{ type: 'text', content: 'partial' }]

  writeStoredStreamSnapshot('chat-a', items, storage)

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), true)
  assert.equal(storage.map.has(legacyStreamSnapshotKey('chat-a')), false)
})

test('stream snapshot ignores empty writes so reconnect reset keeps visible cache', () => {
  const storage = makeStorage()
  const items = [{ type: 'tool', tool: 'Bash', status: 'running' }]

  writeStoredStreamSnapshot('chat-a', items, storage)
  writeStoredStreamSnapshot('chat-a', [], storage)

  assert.deepEqual(readStoredStreamSnapshot('chat-a', storage), items)
})

test('clear removes current and legacy keys', () => {
  const storage = makeStorage()
  storage.setItem(streamSnapshotKey('chat-a'), JSON.stringify([{ type: 'text', content: 'new' }]))
  storage.setItem(legacyStreamSnapshotKey('chat-a'), JSON.stringify([{ type: 'text', content: 'old' }]))

  clearStoredStreamSnapshot('chat-a', storage)

  assert.equal(storage.map.has(streamSnapshotKey('chat-a')), false)
  assert.equal(storage.map.has(legacyStreamSnapshotKey('chat-a')), false)
})

test('read returns [] for corrupt or absent values', () => {
  const storage = makeStorage()
  storage.setItem(streamSnapshotKey('bad'), '{nope')

  assert.deepEqual(readStoredStreamSnapshot('missing', storage), [])
  assert.deepEqual(readStoredStreamSnapshot('bad', storage), [])
})
