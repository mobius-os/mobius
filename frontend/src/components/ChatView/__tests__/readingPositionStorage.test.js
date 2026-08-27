import { after, test } from 'node:test'
import assert from 'node:assert/strict'

class MemoryStorage {
  constructor() {
    this.values = new Map()
  }

  getItem(key) {
    return this.values.has(String(key)) ? this.values.get(String(key)) : null
  }

  setItem(key, value) {
    this.values.set(String(key), String(value))
  }

  removeItem(key) {
    this.values.delete(String(key))
  }
}

const previousLocalStorage = globalThis.localStorage
const storage = new MemoryStorage()
storage.setItem('chat-reading-position', JSON.stringify({
  'chat-1': {
    kind: 'ANCHOR_AT',
    key: 'client-message',
    offset: -20,
    part: [3],
    at: 1,
  },
}))
globalThis.localStorage = storage

const {
  remapSavedReadingAnchor,
  retireSavedReadingPosition,
  savedReadingAnchorHasNestedPart,
  savedReadingAnchorKey,
} = await import('../scroll/readingPositions.js')

after(() => {
  if (previousLocalStorage === undefined) delete globalThis.localStorage
  else globalThis.localStorage = previousLocalStorage
})

test('saved reading aliases remap once and confirmed absence retires once', () => {
  assert.equal(savedReadingAnchorKey('chat-1'), 'client-message')
  assert.equal(savedReadingAnchorHasNestedPart('chat-1'), true)
  assert.equal(
    remapSavedReadingAnchor('chat-1', 'client-message', 'server-message'),
    true,
  )
  assert.equal(savedReadingAnchorKey('chat-1'), 'server-message')

  const remapped = JSON.parse(storage.getItem('chat-reading-position'))['chat-1']
  assert.equal(remapped.key, 'server-message')
  assert.equal(remapped.offset, -20)
  assert.deepEqual(remapped.part, [3])

  assert.equal(retireSavedReadingPosition('chat-1'), true)
  assert.equal(retireSavedReadingPosition('chat-1'), false)
  assert.equal(savedReadingAnchorKey('chat-1'), null)
  assert.equal(savedReadingAnchorHasNestedPart('chat-1'), false)
  assert.deepEqual(JSON.parse(storage.getItem('chat-reading-position')), {})
})
