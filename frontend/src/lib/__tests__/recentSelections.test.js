import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearRecentSelections,
  readRecentSelections,
  rememberRecentDestination,
  rememberRecentSelection,
  resolveRecentSelections,
} from '../recentSelections.js'

class MemoryStorage {
  constructor(initial = {}) {
    this.values = new Map(Object.entries(initial))
    this.writeCount = 0
  }

  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) {
    this.writeCount += 1
    this.values.set(key, String(value))
  }
  removeItem(key) { this.values.delete(key) }
}

test('selection history is deduplicated in most-recent-first order', () => {
  const storage = new MemoryStorage()
  assert.deepEqual(readRecentSelections(storage), [])

  rememberRecentSelection({ kind: 'chat', id: 'chat-1' }, storage)
  rememberRecentSelection({ kind: 'app', id: 42 }, storage)
  rememberRecentSelection({ kind: 'chat', id: 'chat-1' }, storage)

  assert.deepEqual(readRecentSelections(storage), [
    { kind: 'chat', id: 'chat-1' },
    { kind: 'app', id: '42' },
  ])

  for (let index = 0; index < 14; index += 1) {
    rememberRecentSelection({ kind: 'chat', id: `chat-${index}` }, storage)
  }
  const bounded = readRecentSelections(storage)
  assert.equal(bounded.length, 12)
  assert.deepEqual(bounded.slice(0, 2), [
    { kind: 'chat', id: 'chat-13' },
    { kind: 'chat', id: 'chat-12' },
  ])

  clearRecentSelections(storage)
  assert.deepEqual(readRecentSelections(storage), [])
})

test('shell destinations feed one shared history while non-destinations stay inert', () => {
  const storage = new MemoryStorage()

  rememberRecentDestination({ view: 'chat', chatId: 'chat-1' }, storage)
  rememberRecentDestination({ view: 'apps', appId: 9 }, storage)
  rememberRecentDestination({ view: 'canvas', appId: 9 }, storage)
  rememberRecentDestination({ view: 'settings', chatId: 'chat-2' }, storage)
  rememberRecentDestination({ view: 'chat', chatId: 'chat-1' }, storage)

  assert.deepEqual(readRecentSelections(storage), [
    { kind: 'chat', id: 'chat-1' },
    { kind: 'app', id: '9' },
  ])
  assert.equal(storage.writeCount, 3)
})

test('selection history resolves current items in MRU order and omits missing ones', () => {
  const chats = [
    { id: 'chat-1', title: 'First chat' },
    { id: 'chat-2', title: 'Second chat' },
  ]
  const apps = [{ id: 7, name: 'Memory' }]

  assert.deepEqual(resolveRecentSelections([
    { kind: 'app', id: '7' },
    { kind: 'chat', id: 'missing' },
    { kind: 'chat', id: 'chat-2' },
  ], chats, apps), [
    { kind: 'app', value: apps[0] },
    { kind: 'chat', value: chats[1] },
  ])
})
