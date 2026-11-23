// A key file dropped onto its box is read into the box, never opened by the
// browser or handed to the chat composer.
import test from 'node:test'
import assert from 'node:assert/strict'
import { keyFileDropProps, MAX_KEY_FILE } from '../keyFileDrop.js'

function drop(file) {
  const calls = []
  return {
    calls,
    event: { preventDefault: () => calls.push('preventDefault'), dataTransfer: { files: file ? [file] : [], dropEffect: '' } },
  }
}

test('a dropped key file fills the box with its text', async () => {
  let text = null
  const props = keyFileDropProps(value => { text = value })
  assert.equal(props['data-own-file-drop'], '')
  const { event, calls } = drop({ size: 30, text: async () => '-----BEGIN KEY-----\nabc\n-----END KEY-----\n' })
  props.onDragOver(event)
  props.onDrop(event)
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(text, '-----BEGIN KEY-----\nabc\n-----END KEY-----')
  assert.deepEqual(calls, ['preventDefault', 'preventDefault'])
})

test('a file too large to be a key is refused', () => {
  let error = null
  const props = keyFileDropProps(() => assert.fail('not read'), reason => { error = reason })
  props.onDrop(drop({ size: MAX_KEY_FILE + 1, text: async () => 'x' }).event)
  assert.match(error, /too large/)
})
