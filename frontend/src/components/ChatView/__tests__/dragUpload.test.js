import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createFileDragHandlers,
  dataTransferHasFiles,
  droppedFiles,
} from '../dragUpload.js'

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

function dragEvent(dataTransfer) {
  const calls = []
  return {
    dataTransfer,
    calls,
    preventDefault() { calls.push('preventDefault') },
    stopPropagation() { calls.push('stopPropagation') },
  }
}

test('drag handlers preserve nested depth and clear a quick enter/leave', () => {
  let depth = 0
  const active = []
  const handlers = createFileDragHandlers({
    getDepth: () => depth,
    setDepth: value => { depth = value },
    setActive: value => active.push(value),
    onFiles: () => assert.fail('leave must not attach files'),
  })
  const transfer = { types: ['Files'], files: [] }

  handlers.onDragEnter(dragEvent(transfer))
  handlers.onDragEnter(dragEvent(transfer))
  assert.equal(depth, 2)
  handlers.onDragLeave(dragEvent(transfer))
  assert.equal(depth, 1)
  assert.deepEqual(active, [true, true])
  handlers.onDragLeave(dragEvent(transfer))
  assert.equal(depth, 0)
  assert.deepEqual(active, [true, true, false])
})

test('drop clears the overlay and attaches files exactly once', () => {
  let depth = 2
  const active = []
  const attached = []
  const handlers = createFileDragHandlers({
    getDepth: () => depth,
    setDepth: value => { depth = value },
    setActive: value => active.push(value),
    onFiles: files => attached.push(files),
  })
  const first = { name: 'notes.pdf' }
  const second = { name: 'photo.png' }
  const transfer = { types: ['Files'], files: [first, second] }
  const over = dragEvent(transfer)
  const drop = dragEvent(transfer)

  handlers.onDragOver(over)
  assert.equal(transfer.dropEffect, 'copy')
  assert.deepEqual(over.calls, ['preventDefault', 'stopPropagation'])
  handlers.onDrop(drop)
  assert.equal(depth, 0)
  assert.deepEqual(active, [false])
  assert.deepEqual(attached, [[first, second]])
  assert.deepEqual(drop.calls, ['preventDefault', 'stopPropagation'])
})
