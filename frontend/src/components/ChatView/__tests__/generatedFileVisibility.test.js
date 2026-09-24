import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)

const {
  attachmentIsGalleryImage,
  generatedFileCanPreview,
} = await vite.ssrLoadModule(
  '/src/components/ChatView/Attachments.jsx',
)

const { default: ActiveAssistantSurface } = await vite.ssrLoadModule(
  '/src/components/ChatView/ActiveAssistantSurface.jsx',
)

after(() => vite.close())

const generatedMessage = {
  role: 'assistant',
  content: '',
  blocks: [{
    type: 'generated_files',
    files: [{
      name: 'report.pdf',
      size: 700,
      mime_type: 'application/pdf',
    }],
  }],
}

function renderGeneratedMessage(isStreaming) {
  return renderToStaticMarkup(createElement(MsgContent, {
    msg: generatedMessage,
    chatId: 'chat-generated-file',
    isLastMsg: true,
    isStreaming,
  }))
}

test('generated-file card stays hidden while the assistant is writing', () => {
  const html = renderGeneratedMessage(true)

  assert.doesNotMatch(html, /chat__attach-file/)
  assert.doesNotMatch(html, />report\.pdf</)
})

test('generated-file card appears after the assistant response settles', () => {
  const html = renderGeneratedMessage(false)

  assert.match(html, /chat__attach-file/)
  assert.match(html, />report\.pdf</)
})

test('generated images retain the existing inline gallery treatment', () => {
  const imageMessage = {
    ...generatedMessage,
    blocks: [{ type: 'generated_files', files: [{
      name: 'chart.png', size: 900, mime_type: 'image/png', previewable: true,
    }] }],
  }
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: imageMessage,
    chatId: 'chat-generated-file',
    isLastMsg: true,
    isStreaming: false,
  }))

  assert.doesNotMatch(html, /chat__attach-file/)
  assert.match(html, /chat__attach-images/)
  assert.match(html, /chat__attach-thumb-frame/)
})

test('only browser-safe generated documents open as previews', () => {
  assert.equal(generatedFileCanPreview({
    kind: 'generated', previewable: true,
  }), true)
  assert.equal(generatedFileCanPreview({
    kind: 'generated', previewable: false,
  }), false)
  assert.equal(generatedFileCanPreview({
    kind: 'generated', mime_type: 'application/pdf',
  }), false)
  assert.equal(attachmentIsGalleryImage({
    kind: 'generated', mime_type: 'image/png', previewable: true,
  }), true)
  assert.equal(attachmentIsGalleryImage({
    kind: 'generated', mime_type: 'image/svg+xml', previewable: false,
  }), false)
})

for (const isStreaming of [true, false]) {
  test(`projected peer activity and durable downloads survive the active handoff (${isStreaming})`, () => {
    const rawBlocks = generatedMessage.blocks
    const peer = {
      type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-review',
      status: 'done', input: '', output: '',
    }
    const html = renderToStaticMarkup(createElement(ActiveAssistantSurface, {
      activeMirrorMsg: { ...generatedMessage, blocks: [peer, ...rawBlocks] },
      activitySourceBlocks: rawBlocks,
      useDbActivePayload: false,
      hasLivePayload: true,
      streamItems: [{
        type: 'tool', tool: 'Bash', tool_use_id: 'tool-pdf', status: 'done',
      }],
      chatId: 'chat-generated-file', dataKey: 'assistant-file', isStreaming,
    }))
    assert.match(html, /Exchang(?:ing|ed) messages/i)
    assert.equal(html.includes('chat__attach-file'), !isStreaming)
  })
}
