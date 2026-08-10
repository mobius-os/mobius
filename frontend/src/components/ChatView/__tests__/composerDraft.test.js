import { test } from 'node:test'
import assert from 'node:assert/strict'
import 'fake-indexeddb/auto'

import {
  _clearComposerDraftMemoryForTests,
  clearComposerDraft,
  clearDurableComposerDrafts,
  consumeComposerHandoff,
  flushComposerDraftPersistence,
  persistComposerDraft,
  readComposerHandoff,
  readComposerDraft,
  readComposerDraftAsync,
  stageComposerHandoff,
} from '../composerDraft.js'
import { streamSnapshotKey } from '../streamSnapshotCache.js'

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
  assert.equal(persistComposerDraft('chat-a', 'unfinished thought', [], storage), true)
  const stored = JSON.parse(storage.getItem('draft:chat-a'))
  assert.equal(stored.type, 'mobius-composer-draft')
  assert.equal(stored.version, 2)
  assert.equal(stored.input, 'unfinished thought')
  assert.equal(Number.isFinite(stored.updated_at), true)
  assert.deepEqual(readComposerDraft('chat-a', storage), {
    input: 'unfinished thought',
    attachments: [],
  })

  assert.equal(persistComposerDraft('chat-a', '', [], storage), true)
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

test('chat handoffs keep exact draft and autosend text under one owner', () => {
  const storage = storageStub({
    'pending-draft-autosend': 'stale',
    'draft-autosend:chat-a': 'stale',
    'draft-autosend:abandoned-chat': 'abandoned approval',
  })

  assert.equal(stageComposerHandoff('chat-a', 'Review this exactly', {
    attachments: [{
      name: 'review.txt', size: 9, mime_type: 'text/plain', status: 'done',
    }],
    autoSend: true,
    storage,
  }), true)
  assert.deepEqual(readComposerDraft('chat-a', storage), {
    input: 'Review this exactly',
    attachments: [{
      name: 'review.txt', size: 9, mime_type: 'text/plain', status: 'done',
    }],
  })
  assert.equal(storage.getItem('pending-draft'), 'Review this exactly')
  assert.equal(storage.getItem('pending-draft-autosend'), 'Review this exactly')
  assert.equal(storage.getItem('draft-autosend:chat-a'), 'Review this exactly')
  assert.equal(storage.getItem('draft-autosend:abandoned-chat'), null)
  assert.deepEqual(readComposerHandoff('chat-a', storage), {
    draft: 'Review this exactly',
    autoSendDraft: 'Review this exactly',
  })

  consumeComposerHandoff('chat-a', 'Review this exactly', { storage })
  assert.equal(storage.getItem('pending-draft'), null)
  assert.equal(storage.getItem('pending-draft-autosend'), null)
  assert.equal(storage.getItem('draft-autosend:chat-a'), 'Review this exactly',
    'the retry marker survives until the send attempt actually starts')
  assert.deepEqual(readComposerHandoff('chat-a', storage), {
    draft: null,
    autoSendDraft: 'Review this exactly',
  })
  consumeComposerHandoff('chat-a', 'Review this exactly', { autoSend: true, storage })
  assert.equal(storage.getItem('draft-autosend:chat-a'), null)

  assert.equal(stageComposerHandoff('chat-a', 'Leave this for review', { storage }), true)
  consumeComposerHandoff('chat-a', 'Review this exactly', { autoSend: true, storage })
  assert.equal(storage.getItem('pending-draft'), 'Leave this for review')
  assert.equal(storage.getItem('pending-draft-autosend'), null)
  assert.equal(storage.getItem('draft-autosend:chat-a'), null)

  assert.equal(stageComposerHandoff('attachment-only', '', {
    attachments: [{
      name: 'reference.png', size: 12, mime_type: 'image/png', status: 'done',
    }],
    storage,
  }), true)
  assert.deepEqual(readComposerDraft('attachment-only', storage), {
    input: '',
    attachments: [{
      name: 'reference.png', size: 12, mime_type: 'image/png', status: 'done',
    }],
  })

  persistComposerDraft('cleared-handoff', 'remove me', [], storage)
  assert.equal(stageComposerHandoff('cleared-handoff', '', {
    allowEmpty: true,
    storage,
  }), true)
  assert.equal(storage.getItem('pending-draft'), null)
  assert.equal(storage.getItem('draft-autosend:chat-a'), null)
  assert.deepEqual(readComposerDraft('cleared-handoff', storage), {
    input: '', attachments: [],
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

test('quota recovery sacrifices only transient cache and keeps every owner draft', async () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const storage = storageStub()
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: storage,
  })

  try {
    await clearDurableComposerDrafts()

    persistComposerDraft('chat-a', 'older owner draft')
    storage.setItem(streamSnapshotKey('running-chat'), JSON.stringify([
      { type: 'text', content: 'regenerable partial' },
    ]))

    const normalSet = storage.setItem.bind(storage)
    storage.setItem = (key, value) => {
      if (key === 'draft:chat-b' && storage.getItem(streamSnapshotKey('running-chat'))) {
        const error = new Error('full')
        error.name = 'QuotaExceededError'
        throw error
      }
      normalSet(key, value)
    }

    assert.equal(persistComposerDraft('chat-b', 'new owner draft'), true)
    assert.equal(storage.getItem(streamSnapshotKey('running-chat')), null)
    assert.equal(readComposerDraft('chat-a').input, 'older owner draft')
    assert.equal(readComposerDraft('chat-b').input, 'new owner draft')

    // If Web Storage remains unavailable even after cache reclamation, the
    // dedicated draft database still survives a document-memory reset. Rapid
    // updates are coalesced latest-wins rather than queuing one transaction per
    // keystroke.
    storage.setItem = (key, value) => {
      if (key === 'draft:chat-c') {
        const error = new Error('still full')
        error.name = 'QuotaExceededError'
        throw error
      }
      normalSet(key, value)
    }
    persistComposerDraft('chat-c', 'one')
    persistComposerDraft('chat-c', 'one two')
    persistComposerDraft('chat-c', 'one two three')
    await flushComposerDraftPersistence()

    _clearComposerDraftMemoryForTests()
    assert.equal(storage.getItem('draft:chat-c'), null)
    assert.deepEqual(await readComposerDraftAsync('chat-c'), {
      input: 'one two three',
      attachments: [],
    })

    // Session may contain an older successful write while later writes fit
    // only in IndexedDB. Even several changes inside one wall-clock tick carry
    // a strict order, so hydration selects the actual latest value.
    persistComposerDraft('chat-d', 'old session copy')
    storage.setItem = (key, value) => {
      if (key === 'draft:chat-d') {
        const error = new Error('full again')
        error.name = 'QuotaExceededError'
        throw error
      }
      normalSet(key, value)
    }
    const realNow = Date.now
    Date.now = () => 1_000
    try {
      persistComposerDraft('chat-d', 'newer durable copy')
      persistComposerDraft('chat-d', 'newest durable copy')
    } finally {
      Date.now = realNow
    }
    await flushComposerDraftPersistence()
    _clearComposerDraftMemoryForTests()
    assert.equal(readComposerDraft('chat-d').input, 'old session copy')
    assert.equal(
      (await readComposerDraftAsync('chat-d')).input,
      'newest durable copy',
    )
    assert.equal(readComposerDraft('chat-d').input, 'newest durable copy')

    clearComposerDraft('chat-d')
    await flushComposerDraftPersistence()
    _clearComposerDraftMemoryForTests()
    assert.deepEqual(await readComposerDraftAsync('chat-d'), {
      input: '',
      attachments: [],
    })

    await clearDurableComposerDrafts()
    _clearComposerDraftMemoryForTests()
    assert.deepEqual(await readComposerDraftAsync('chat-c'), {
      input: '',
      attachments: [],
    })
  } finally {
    await clearDurableComposerDrafts()
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  }
})
