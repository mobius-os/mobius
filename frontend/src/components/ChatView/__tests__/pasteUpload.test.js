import test from 'node:test'
import assert from 'node:assert/strict'

import {
  filePasteNeedsDefaultPrevented,
  insertClipboardText,
  pastedFiles,
  readClipboardContents,
} from '../pasteUpload.js'

test('pastedFiles reads screenshots from clipboard files', () => {
  const image = { name: 'screenshot.png', type: 'image/png' }
  assert.deepEqual(pastedFiles({ files: [image], items: [] }), [image])
})

test('pastedFiles falls back to clipboard file items', () => {
  const image = { name: 'clipboard.png', type: 'image/png' }
  const clipboard = {
    files: [],
    items: [
      { kind: 'string', getAsFile: () => null },
      { kind: 'file', getAsFile: () => image },
    ],
  }
  assert.deepEqual(pastedFiles(clipboard), [image])
})

test('file paste preserves accompanying text but suppresses file-only insertion', () => {
  const files = [{ name: 'shot.png' }]
  assert.equal(filePasteNeedsDefaultPrevented({ getData: () => '' }, files), true)
  assert.equal(filePasteNeedsDefaultPrevented({ getData: () => 'caption' }, files), false)
})

function clipboardItem(types, values) {
  return {
    types,
    getType: async (type) => new Blob([values[type]], { type }),
  }
}

test('explicit clipboard read uses one semantic representation per item', async () => {
  const clipboard = {
    read: async () => [
      clipboardItem(
        ['text/plain', 'image/png'],
        { 'text/plain': 'duplicate image label', 'image/png': 'png-bytes' },
      ),
      clipboardItem(['text/plain'], { 'text/plain': 'A caption' }),
      clipboardItem(['application/pdf'], { 'application/pdf': 'pdf-bytes' }),
    ],
  }

  const result = await readClipboardContents(clipboard)

  assert.equal(result.status, 'ready')
  assert.equal(result.text, 'A caption')
  assert.deepEqual(
    result.files.map(file => [file.name, file.type]),
    [
      ['clipboard-image.png', 'image/png'],
      ['clipboard-file-2.pdf', 'application/pdf'],
    ],
  )
})

test('explicit clipboard read falls back to text-only browser support', async () => {
  const result = await readClipboardContents({ readText: async () => 'plain text' })
  assert.deepEqual(result, { status: 'ready', files: [], text: 'plain text' })
})

test('explicit clipboard read reports permission and empty outcomes', async () => {
  const denied = new Error('blocked')
  denied.name = 'NotAllowedError'
  assert.deepEqual(
    await readClipboardContents({ read: async () => { throw denied } }),
    { status: 'denied', files: [], text: '' },
  )
  assert.deepEqual(
    await readClipboardContents({ read: async () => [] }),
    { status: 'empty', files: [], text: '' },
  )
})

test('clipboard text replaces the current selection and returns its next caret', () => {
  assert.deepEqual(
    insertClipboardText('hello world', 'Möbius', 6, 11),
    { value: 'hello Möbius', caret: 12 },
  )
  assert.deepEqual(
    insertClipboardText('draft', '!', undefined, undefined),
    { value: 'draft!', caret: 6 },
  )
})
