import test from 'node:test'
import assert from 'node:assert/strict'

import {
  filePasteNeedsDefaultPrevented,
  pastedFiles,
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
