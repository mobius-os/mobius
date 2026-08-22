import test from 'node:test'
import assert from 'node:assert/strict'
import { dataTransferHasFiles, droppedFiles } from '../dragUpload.js'

test('recognizes Windows Explorer-style file drags before drop', () => {
  assert.equal(dataTransferHasFiles({ types: ['Files'], files: [] }), true)
  assert.equal(dataTransferHasFiles({ types: ['text/plain'], files: [] }), false)
})

test('falls back to file items when drag types are unavailable', () => {
  assert.equal(dataTransferHasFiles({ items: [{ kind: 'file' }] }), true)
  assert.equal(dataTransferHasFiles({ items: [{ kind: 'string' }] }), false)
})

test('extracts every real file from a completed drop', () => {
  const first = { name: 'notes.pdf' }
  const second = { name: 'photo.png' }
  assert.deepEqual(droppedFiles({ files: [first, null, second] }), [first, second])
  assert.deepEqual(droppedFiles(null), [])
})
