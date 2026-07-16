import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { persistComposerDraft } from '../composerDraft.js'

function storageStub(initial = {}) {
  const values = new Map(Object.entries(initial))
  return {
    get length() { return values.size },
    key(index) { return [...values.keys()][index] ?? null },
    getItem(key) { return values.get(key) ?? null },
    setItem(key, value) { values.set(key, String(value)) },
    removeItem(key) { values.delete(key) },
  }
}

test('persists and clears a chat draft synchronously', () => {
  const storage = storageStub()
  assert.equal(persistComposerDraft('chat-a', 'unfinished thought', storage), true)
  assert.equal(storage.getItem('draft:chat-a'), 'unfinished thought')

  assert.equal(persistComposerDraft('chat-a', '', storage), true)
  assert.equal(storage.getItem('draft:chat-a'), null)
})

test('evicts one draft and retries when session storage is full', () => {
  const storage = storageStub({ 'draft:chat-a': 'older' })
  const normalSet = storage.setItem.bind(storage)
  let first = true
  storage.setItem = (key, value) => {
    if (first) {
      first = false
      const error = new Error('full')
      error.name = 'QuotaExceededError'
      throw error
    }
    normalSet(key, value)
  }

  assert.equal(persistComposerDraft('chat-b', 'latest', storage), true)
  assert.equal(storage.getItem('draft:chat-a'), null)
  assert.equal(storage.getItem('draft:chat-b'), 'latest')
})

test('the composer state boundary saves before scheduling React state', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('function setComposerInput(nextInput)')
  const end = source.indexOf('\n  }', start)
  const body = source.slice(start, end)

  const save = body.indexOf('persistComposerDraft(chatId, nextInput)')
  const render = body.indexOf('setInputState(nextInput)')
  assert.ok(save >= 0, 'all composer changes must be persisted directly')
  assert.ok(render > save, 'draft persistence must happen before navigation can unmount React')
})
