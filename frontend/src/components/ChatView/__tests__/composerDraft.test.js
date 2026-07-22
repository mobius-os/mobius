import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { persistComposerDraft, readComposerDraft } from '../composerDraft.js'

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

test('persists uploaded attachments with text and restores a sendable draft', () => {
  const storage = storageStub()
  const attachments = [{
    id: 'local-only',
    name: 'map.png',
    size: 1096340,
    mime_type: 'image/png',
    objectUrl: 'blob:temporary-and-non-restorable',
    status: 'done',
  }]

  assert.equal(
    persistComposerDraft('chat-map', 'What is this?', attachments, storage),
    true,
  )
  assert.deepEqual(readComposerDraft('chat-map', storage), {
    input: 'What is this?',
    attachments: [{
      name: 'map.png',
      size: 1096340,
      mime_type: 'image/png',
      status: 'done',
    }],
  })
  assert.equal(storage.getItem('draft:chat-map').includes('blob:temporary'), false)
})

test('a restored attachment survives the mount persistence pass', () => {
  const storage = storageStub()
  persistComposerDraft('chat-remount', 'Keep this', [{
    name: 'draft-note.txt', size: 16, mime_type: 'text/plain', status: 'done',
  }], storage)

  const firstMount = readComposerDraft('chat-remount', storage)
  assert.deepEqual(firstMount.attachments.map(a => a.status), ['done'])
  persistComposerDraft(
    'chat-remount', firstMount.input, firstMount.attachments, storage,
  )

  assert.deepEqual(readComposerDraft('chat-remount', storage).attachments, [{
    name: 'draft-note.txt', size: 16, mime_type: 'text/plain', status: 'done',
  }])
})

test('keeps legacy plain-text drafts readable', () => {
  const storage = storageStub({ 'draft:legacy': 'unfinished thought' })
  assert.deepEqual(readComposerDraft('legacy', storage), {
    input: 'unfinished thought',
    attachments: [],
  })
})

test('does not restore attachments that never finished uploading', () => {
  const storage = storageStub()
  persistComposerDraft('chat-a', 'draft', [
    { name: 'ready.png', status: 'done', mime_type: 'image/png', size: 1 },
    { name: 'still-uploading.png', status: 'uploading', mime_type: 'image/png', size: 2 },
    { name: 'failed.png', status: 'error', mime_type: 'image/png', size: 3 },
    { name: 'future-state.png', status: 'processing', mime_type: 'image/png', size: 4 },
  ], storage)
  assert.deepEqual(
    readComposerDraft('chat-a', storage).attachments.map(a => a.name),
    ['ready.png'],
  )
})

test('rejects status-bearing attachments injected into a stored envelope', () => {
  const storage = storageStub({
    'draft:chat-a': JSON.stringify({
      type: 'mobius-composer-draft',
      version: 1,
      input: 'draft',
      attachments: [
        { name: 'safe.txt', size: 1, mime_type: 'text/plain' },
        { name: 'unknown.txt', status: 'processing', size: 2, mime_type: 'text/plain' },
      ],
    }),
  })
  assert.deepEqual(
    readComposerDraft('chat-a', storage).attachments.map(a => a.name),
    ['safe.txt'],
  )
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

  assert.equal(persistComposerDraft('chat-b', 'latest', [], storage), true)
  assert.equal(storage.getItem('draft:chat-a'), null)
  assert.equal(storage.getItem('draft:chat-b'), 'latest')
})

test('the composer state boundary saves before scheduling React state', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('function setComposerInput(nextInput)')
  const end = source.indexOf('\n  }', start)
  const body = source.slice(start, end)

  const save = body.indexOf('persistComposerDraft(chatId, nextInput, draftAttachmentsRef.current)')
  const render = body.indexOf('setInputState(nextInput)')
  assert.ok(save >= 0, 'all composer changes must be persisted directly')
  assert.ok(render > save, 'draft persistence must happen before navigation can unmount React')
})
