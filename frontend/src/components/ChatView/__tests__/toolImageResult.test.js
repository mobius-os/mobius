import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  chatImageReference,
  generatedImageReference,
  imagePathFromInput,
  inlineImageReference,
  scratchImageReference,
  sameImageReference,
  savedStepImageReference,
  servedImageReference,
  temporaryImageReference,
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

test('a viewed /tmp image resolves through the owning chat only', () => {
  assert.deepEqual(
    temporaryImageReference('/tmp/visuals/render one.png', 'chat-123'),
    {
      kind: 'tmp',
      chatId: 'chat-123',
      filename: 'visuals/render one.png',
    },
  )
  assert.deepEqual(
    servedImageReference(
      JSON.stringify({ path: '/tmp/inspect.png', detail: 'original' }),
      'chat-123',
    ),
    { kind: 'tmp', chatId: 'chat-123', filename: 'inspect.png' },
  )
  assert.equal(temporaryImageReference('/tmp/visual.png', ''), null)
  assert.equal(temporaryImageReference('/var/tmp/visual.png', 'chat-123'), null)
})

test('a viewed generated image resolves only to matching final attachment bytes', () => {
  const path = '/data/chats/chat-123/deliverables/inbox/image.png'
  const digest = 'a'.repeat(64)
  const files = [
    { name: 'image.png', mime_type: 'image/png', previewable: true, sha256: 'b'.repeat(64) },
    { name: 'image_1.png', mime_type: 'image/png', previewable: true, sha256: digest },
  ]
  const options = { files, viewedDigest: digest, completed: true }
  const expected = {
    kind: 'generated', chatId: 'chat-123', collection: 'generated-files',
    filename: 'image_1.png', expectedSha256: digest,
  }
  assert.deepEqual(generatedImageReference(path, 'chat-123', options), expected)
  assert.equal(generatedImageReference(path, 'chat-123', { files, viewedDigest: digest }), null)
  assert.deepEqual(servedImageReference(path, 'chat-123', options), expected)
  assert.equal(generatedImageReference(path, 'chat-123', { files, viewedDigest: 'c'.repeat(64), completed: true }), null)
  assert.equal(generatedImageReference(path, 'chat-123', { files, viewedDigest: '', completed: true }), null)
  assert.equal(generatedImageReference(path, 'chat-123', { files }), null)
  assert.equal(generatedImageReference(path, 'another-chat', options), null)
  assert.equal(generatedImageReference('/data/chats/chat-123/deliverables/files/secret.png', 'chat-123', options), null)
  assert.equal(generatedImageReference('/data/chats/chat-123/deliverables/inbox/nested/x.png', 'chat-123', options), null)
  assert.equal(generatedImageReference('/other/chats/chat-123/deliverables/inbox/image.png', 'chat-123', options), null)
})

test('a saved view with no file fingerprint still requires a serve-time byte match', () => {
  const digest = 'a'.repeat(64)
  const path = '/data/chats/chat-123/deliverables/inbox/image.png'
  const files = [{ name: 'image.png', mime_type: 'image/png', previewable: true }]
  assert.deepEqual(generatedImageReference(path, 'chat-123', { files, viewedDigest: digest, completed: true }), {
    kind: 'generated', chatId: 'chat-123', collection: 'generated-files',
    filename: 'image.png', expectedSha256: digest,
  })
  assert.equal(generatedImageReference(path, 'chat-123', {
    files: [{ ...files[0], sha256: 'b'.repeat(64) }], viewedDigest: digest, completed: true,
  }), null)
})

test('a historical image view without a fingerprint cannot claim a preview', () => {
  const path = '/data/chats/chat-123/deliverables/inbox/image.png'
  const files = [{ name: 'image.png', mime_type: 'image/png', previewable: true }]
  assert.equal(generatedImageReference(path, 'chat-123', { files, completed: true }), null)
})

test('a viewed agent-scratch image resolves only for the same chat', () => {
  const path = '/data/agent-scratch/chat-123/renders/preview one.png'
  assert.deepEqual(scratchImageReference(path, 'chat-123'), {
    kind: 'scratch',
    chatId: 'chat-123',
    filename: 'renders/preview one.png',
  })
  assert.deepEqual(servedImageReference(JSON.stringify({ path }), 'chat-123'),
    scratchImageReference(path, 'chat-123'))
  assert.equal(scratchImageReference(path, 'another-chat'), null)
  assert.equal(scratchImageReference(path, ''), null)
  assert.equal(scratchImageReference('/data/agent-scratch-other/chat-123/a.png', 'chat-123'), null)
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
    toolImageReference('/tmp/visual.png', output, 'chat-123'),
    { kind: 'tmp', chatId: 'chat-123', filename: 'visual.png' },
  )
  assert.deepEqual(
    toolImageReference('/var/tmp/visual.png', output, 'chat-123'),
    { kind: 'inline', src: 'data:image/png;base64,aGVsbG8=' },
  )
  assert.equal(
    servedImageReference('/data/apps/example-app/icon.png'),
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
  const preview = readFileSync(
    new URL('../useToolImagePreview.js', import.meta.url),
    'utf8',
  )

  assert.doesNotMatch(result + preview, /apiFetch|\/apps\//)
  assert.match(preview, /status: 'failed'/)
  assert.match(result, /current\.status !== 'ready'/)
  assert.doesNotMatch(result, /useEffect|useState/)
  assert.match(trigger, /onError=\{onError\}/)
})

test('an interrupted screenshot keeps its result text; only a saved capture becomes an image', async () => {
  const React = await import('react')
  const { renderToStaticMarkup } = await import('react-dom/server')
  const { createServer } = await import('vite')
  const vite = await createServer({
    appType: 'custom', logLevel: 'error',
    server: { middlewareMode: true, hmr: false, ws: false },
    ssr: { noExternal: ['@openai/apps-sdk-ui'] },
  })
  try {
    const { default: ToolBlock } = await vite.ssrLoadModule('/src/components/ChatView/ToolBlock.jsx')
    const { persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
    const chatId = 'screenshot-stop-test'
    const disclosureKey = 'screenshot-step'
    persistDisclosureOpen(chatId, disclosureKey, true)
    const block = {
      type: 'tool', tool: 'mcp__mobius_control__screenshot', input: 'app_id=9',
      output: 'Stopped', status: 'done', tool_use_id: 'shot-stop',
    }
    const render = t => renderToStaticMarkup(React.createElement(ToolBlock, {
      t, chatId, disclosureKey,
    }))

    for (const t of [block, { ...block, saved_image: '../other.png' }]) {
      const html = render(t)
      assert.match(html, /Stopped/)
      assert.match(html, /Copy output/)
      assert.doesNotMatch(html, /chat__tool--image|Image preview unavailable/)
    }
    assert.doesNotMatch(render({ ...block, input: '/tmp/unsaved.png' }), /chat__tool--image/)
    assert.doesNotMatch(render({ ...block, saved_image: 'saved.png', output_exit_code: 1 }), /chat__tool--image/)
    assert.doesNotMatch(render({ ...block, saved_image: 'saved.png', status: 'running' }), /chat__tool--image/)
    assert.match(render({ ...block, saved_image: 'saved.png' }), /chat__tool--image/)
    assert.deepEqual(savedStepImageReference('saved.png', chatId), {
      kind: 'chat', chatId, collection: 'media', filename: 'saved.png',
    })
  } finally {
    await vite.close()
  }
})

test('a screenshot step renders its recorded chat-media file without its stored result', () => {
  const saved = {
    kind: 'chat',
    chatId: 'chat-123',
    collection: 'media',
    filename: 'shot-1791457803253833437.png',
  }
  const step = { savedImage: 'shot-1791457803253833437.png' }
  // Served means ToolBlock skips the stored-result download entirely.
  assert.deepEqual(servedImageReference('app_id=9', 'chat-123', step), saved)
  assert.deepEqual(toolImageReference('route=/shell/?app=9', undefined, 'chat-123', step), saved)
})

test('a recorded screenshot name never escapes this chat\'s media', () => {
  assert.equal(savedStepImageReference('../uploads/secret.png', 'chat-123'), null)
  assert.equal(savedStepImageReference('nested/shot.png', 'chat-123'), null)
  assert.equal(savedStepImageReference('', 'chat-123'), null)
  assert.equal(savedStepImageReference(undefined, 'chat-123'), null)
  assert.equal(savedStepImageReference('shot.png', ''), null)
  assert.equal(servedImageReference('app_id=9', 'chat-123'), null,
    'an unrecorded screenshot step is not served')
})

test('a rebuilt reference to the same picture counts as the same image', () => {
  const chat = { kind: 'chat', chatId: 'c1', collection: 'media', filename: 'shot.png' }
  assert.equal(sameImageReference(chat, { ...chat }), true)
  assert.equal(sameImageReference(chat, { ...chat, filename: 'other.png' }), false)
  assert.equal(
    sameImageReference(
      { kind: 'generated', chatId: 'c1', collection: 'generated-files', filename: 'a.png', expectedSha256: 'x' },
      { kind: 'generated', chatId: 'c1', collection: 'generated-files', filename: 'a.png', expectedSha256: 'y' },
    ),
    false,
    'a different fingerprint is a different picture',
  )
  assert.equal(sameImageReference({ kind: 'inline', src: 'data:a' }, { kind: 'inline', src: 'data:b' }), false)
  assert.equal(sameImageReference(null, chat), false)
  assert.equal(sameImageReference(null, null), true)
})
