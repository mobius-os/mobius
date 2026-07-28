import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  chatImageReference,
  durableImageReference,
  imagePathFromInput,
  inlineImageReference,
  toolImageReference,
} from '../toolImageResult.js'

test('a viewed chat-media path resolves to the original protected file', () => {
  assert.deepEqual(
    chatImageReference('/data/chats/chat-123/media/visual proof.png'),
    {
      kind: 'chat',
      chatId: 'chat-123',
      collection: 'media',
      filename: 'visual proof.png',
    },
  )
  assert.deepEqual(
    chatImageReference('/data/chats/chat-123/uploads/input.png'),
    {
      kind: 'chat',
      chatId: 'chat-123',
      collection: 'uploads',
      filename: 'input.png',
    },
  )
  assert.deepEqual(
    chatImageReference(JSON.stringify({
      path: '/data/chats/chat-123/media/inspected.png',
      detail: 'original',
    })),
    {
      kind: 'chat',
      chatId: 'chat-123',
      collection: 'media',
      filename: 'inspected.png',
    },
  )
  assert.equal(
    imagePathFromInput({ file_path: '/data/chats/chat-123/media/object.png' }),
    '/data/chats/chat-123/media/object.png',
  )
})

test('paths outside chat-owned image storage do not become browser URLs', () => {
  assert.equal(chatImageReference('/tmp/visual.png'), null)
  assert.equal(chatImageReference('/data/chats/chat/media/folder/image.png'), null)
  assert.equal(chatImageReference('/data/chats/chat?token=other/media/image.png'), null)
  assert.equal(chatImageReference('/data/apps/private.png'), null)
})

test('a base64 image result is an explicit fallback for non-chat paths', () => {
  const output = JSON.stringify({
    type: 'image',
    source: {
      type: 'base64',
      data: 'aGVsbG8=',
      media_type: 'image/png',
    },
  })
  assert.deepEqual(
    inlineImageReference(output),
    { kind: 'inline', src: 'data:image/png;base64,aGVsbG8=' },
  )
  assert.deepEqual(
    toolImageReference('/tmp/visual.png', output),
    { kind: 'inline', src: 'data:image/png;base64,aGVsbG8=' },
  )
  assert.equal(
    durableImageReference('/data/apps/example-app/icon.png'),
    null,
    'editable app files are not replaced by a potentially different runtime asset',
  )
  assert.deepEqual(
    toolImageReference('/data/apps/example-app/icon.png', output),
    { kind: 'inline', src: 'data:image/png;base64,aGVsbG8=' },
  )
})

test('chat files win over duplicated base64 and unsafe result types are rejected', () => {
  const unsafe = JSON.stringify({
    type: 'image',
    source: {
      type: 'base64',
      data: 'PHN2Zz4=',
      media_type: 'image/svg+xml',
    },
  })
  assert.deepEqual(
    toolImageReference('/data/chats/c/media/safe.png', unsafe),
    { kind: 'chat', chatId: 'c', collection: 'media', filename: 'safe.png' },
  )
  assert.equal(inlineImageReference(unsafe), null)
  assert.equal(inlineImageReference('{"type":"image"'), null)
})

test('image-load failures settle without fetching unrelated app metadata', () => {
  const result = readFileSync(
    new URL('../ToolImageResult.jsx', import.meta.url),
    'utf8',
  )
  const trigger = readFileSync(
    new URL('../ImagePreviewButton.jsx', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(result, /apiFetch|\/apps\//)
  assert.match(result, /onError=\{\(\) => setResolved/)
  assert.match(trigger, /onError=\{onError\}/)
})
