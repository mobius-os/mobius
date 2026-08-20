import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
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

test('the whole active chat feeds dropped files into the existing composer path', () => {
  const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const css = readFileSync(new URL('../ChatView.css', import.meta.url), 'utf8')
  assert.match(chatView, /className={`chat[^`]*`}[\s\S]*?onDragEnter={handleFileDragEnter}[\s\S]*?onDrop={handleFileDrop}/)
  assert.match(chatView, /const files = droppedFiles\(event\.dataTransfer\)[\s\S]*?handleComposerAddFiles\(files\)/)
  assert.match(chatView, /Drop files to attach/)
  assert.match(css, /\.chat__file-drop-target\s*\{[\s\S]*?pointer-events:\s*none/)
})
