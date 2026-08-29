import test from 'node:test'
import assert from 'node:assert/strict'

import {
  applyProjectPreviewStorageRequest,
  PROJECT_PREVIEW_STORAGE_LIMIT,
  projectPreviewStorageKey,
  validProjectPreviewPath,
} from '../projectPreviewStorage.js'

function memoryStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  }
}

test('personal preview storage is scoped by project and source path', () => {
  assert.notEqual(
    projectPreviewStorageKey('project-a', 'index.html'),
    projectPreviewStorageKey('project-b', 'index.html'),
  )
  assert.notEqual(
    projectPreviewStorageKey('project-a', 'index.html'),
    projectPreviewStorageKey('project-a', 'admin.html'),
  )
})

test('personal preview storage supports JSON values without exposing arbitrary keys', () => {
  const storage = memoryStorage()
  const key = projectPreviewStorageKey('project-a', 'index.html')
  const board = { cards: [{ id: 'one', title: 'Test privately' }] }
  assert.deepEqual(applyProjectPreviewStorageRequest(storage, key, {
    method: 'set', path: 'board.json', value: board,
  }), board)
  assert.deepEqual(applyProjectPreviewStorageRequest(storage, key, {
    method: 'get', path: 'board.json',
  }), board)
  assert.deepEqual(applyProjectPreviewStorageRequest(storage, key, {
    method: 'list', path: '',
  }), ['board.json'])
  assert.equal(validProjectPreviewPath('../owner-token'), false)
})

test('personal preview storage refuses an oversized namespace without replacing prior data', () => {
  const storage = memoryStorage()
  const key = projectPreviewStorageKey('project-a', 'index.html')
  applyProjectPreviewStorageRequest(storage, key, { method: 'set', path: 'safe.json', value: { ok: true } })
  assert.throws(() => applyProjectPreviewStorageRequest(storage, key, {
    method: 'set', path: 'huge.json', value: 'x'.repeat(PROJECT_PREVIEW_STORAGE_LIMIT),
  }), /Personal preview data is full/)
  assert.deepEqual(applyProjectPreviewStorageRequest(storage, key, {
    method: 'get', path: 'safe.json',
  }), { ok: true })
})
