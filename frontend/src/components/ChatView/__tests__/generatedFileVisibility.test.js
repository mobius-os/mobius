import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

globalThis.location = { origin: 'http://localhost' }
globalThis.window = { location: globalThis.location, innerHeight: 800 }

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  plugins: [{
    name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub' ? 'export default { sanitize: value => String(value) }' : null,
  }],
})
const { default: MsgContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/MsgContent.jsx',
)

const {
  attachmentIsGalleryImage,
  generatedFileCanPreview,
  generatedFileIsMarkdown,
  generatedFileIsPdf,
  documentAttachmentIdentity,
} = await vite.ssrLoadModule(
  '/src/components/ChatView/Attachments.jsx',
)
const { default: DocumentAttachment, markdownCardExcerpt } = await vite.ssrLoadModule(
  '/src/components/ChatView/DocumentAttachment.jsx',
)

const { default: AssistantReply } = await vite.ssrLoadModule(
  '/src/components/ChatView/AssistantReply.jsx',
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
      previewable: true,
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

test('a post-answer generated file does not hide the recovery action', () => {
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: {
      role: 'assistant', content: '', blocks: [
        {
          type: 'error', message: 'Paused for restart.', resumable: true,
          pause: { kind: 'restart' },
        },
        ...generatedMessage.blocks,
      ],
    },
    chatId: 'chat-generated-file', isLastMsg: true, isStreaming: false,
    onResume() {},
  }))

  assert.match(html, />Resume<\/button>/)
  assert.match(html, />report\.pdf</)
})

test('generated images use full in-chat image frames rather than upload thumbnails', () => {
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
  assert.match(html, /chat__generated-image/)
  assert.match(html, /md-image-frame/)
  assert.doesNotMatch(html, /chat__attach-thumb-frame/)
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
  assert.equal(generatedFileIsMarkdown({
    kind: 'generated', mime_type: 'text/markdown', previewable: false,
  }), true)
  assert.equal(generatedFileIsMarkdown({
    kind: 'generated', mime_type: 'text/html', previewable: false,
  }), false)
  assert.equal(generatedFileIsPdf({
    kind: 'generated', mime_type: 'application/pdf', previewable: true,
  }), true)
  assert.equal(generatedFileIsPdf({
    kind: 'generated', mime_type: 'application/pdf', previewable: false,
  }), false)
})

test('document reader state is isolated by chat and captured attachment bytes', () => {
  const file = { name: 'report.md', sha256: 'a'.repeat(64) }
  const original = documentAttachmentIdentity(file, 'chat-one')
  assert.notEqual(original, documentAttachmentIdentity(file, 'chat-two'))
  assert.notEqual(original, documentAttachmentIdentity({ ...file, sha256: 'b'.repeat(64) }, 'chat-one'))
})

test('generated Markdown offers a content-preview card and Download without changing raw-file policy', () => {
  const markdownMessage = {
    ...generatedMessage,
    blocks: [{ type: 'generated_files', files: [{
      name: 'review.md', size: 51709, mime_type: 'text/markdown', previewable: false,
    }] }],
  }
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: markdownMessage,
    chatId: 'chat-generated-file',
    isLastMsg: true,
    isStreaming: false,
  }))

  assert.match(html, /chat__document-card-paper/)
  assert.match(html, /aria-label="Expand review\.md preview"/)
  assert.match(html, /<button[^>]+aria-label="Download review\.md"/)
  assert.doesNotMatch(html, /preview=true/)
})

test('generated PDF offers a first-page preview card and Download', () => {
  const html = renderGeneratedMessage(false)
  assert.match(html, /chat__document-card-pdf/)
  assert.match(html, /aria-label="Expand report\.pdf preview"/)
  assert.match(html, /<button[^>]+aria-label="Download report\.pdf"/)
  assert.doesNotMatch(html, /target="_blank"/)
})

test('document cards enlarge in chat without losing the original download', () => {
  const file = { name: 'report.pdf', size: 700, mime_type: 'application/pdf' }
  const props = {
    file, chatId: 'chat-generated-file',
    onToggle() {},
  }
  const closed = renderToStaticMarkup(createElement(DocumentAttachment, { ...props, expanded: false }))
  const open = renderToStaticMarkup(createElement(DocumentAttachment, { ...props, expanded: true }))

  assert.match(closed, /chat__document-card-pdf/)
  assert.doesNotMatch(closed, /<iframe/)
  assert.match(closed, /aria-label="Download report\.pdf"/)
  assert.match(open, /chat__document-card--expanded/)
  assert.match(open, />Collapse<\/button>/)
  assert.match(open, /Loading PDF preview/)
  assert.doesNotMatch(open, /Continue in chat/)
  assert.match(open, />Download<\/button>/)
})

test('expanded Markdown joins the shared nested-scroll reader', () => {
  const file = { name: 'review.md', size: 700, mime_type: 'text/markdown' }
  const html = renderToStaticMarkup(createElement(DocumentAttachment, {
    file, chatId: 'chat-generated-file', expanded: true, onToggle() {},
  }))
  assert.match(html, /class="chat__document-card-reader" data-chat-scroll-region/)
})

test('Markdown card shows a readable excerpt of the actual report', () => {
  assert.deepEqual(markdownCardExcerpt('# Review title\n\n## Summary\n**A real finding** with [context](https://example.com).'), {
    title: 'Review title',
    body: 'A real finding with context.',
  })
})

test('generated files appear before a terminal question card in saved chats', () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/shell/' } }
  try {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: {
        role: 'assistant', content: '', blocks: [
          { type: 'text', content: 'Compare these formats.' },
          { type: 'question', question_id: 'which-format', questions: [{
            question: 'Which format feels best?', options: [],
          }] },
          { type: 'generated_files', files: [{
            name: 'review.md', size: 1000, mime_type: 'text/markdown',
          }] },
        ],
      },
      chatId: 'chat-generated-file', isLastMsg: true, isStreaming: false,
    }))

    assert.ok(html.indexOf('Compare these formats.') < html.indexOf('review.md'))
    assert.ok(html.indexOf('review.md') < html.indexOf('Which format feels best?'))
  } finally {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  }
})

test('generated files stay after later prose when a question is not terminal', () => {
  const previousWindow = globalThis.window
  globalThis.window = { location: { href: 'http://localhost/shell/' } }
  try {
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: {
        role: 'assistant', content: '', blocks: [
          { type: 'question', question_id: 'first-question', questions: [{
            question: 'Earlier question?', options: [],
          }] },
          { type: 'text', content: 'Here is the later report.' },
          { type: 'generated_files', files: [{
            name: 'review.md', size: 1000, mime_type: 'text/markdown',
          }] },
        ],
      },
      chatId: 'chat-generated-file', isLastMsg: true, isStreaming: false,
    }))

    assert.ok(html.indexOf('Earlier question?') < html.indexOf('Here is the later report.'))
    assert.ok(html.indexOf('Here is the later report.') < html.indexOf('review.md'))
  } finally {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  }
})

for (const isStreaming of [true, false]) {
  test(`projected peer activity and durable downloads survive the active handoff (${isStreaming})`, () => {
    const rawBlocks = generatedMessage.blocks
    const peer = {
      type: 'tool', tool: 'PeerMessage', tool_use_id: 'peer-review',
      status: 'done', input: '', output: '',
    }
    const html = renderToStaticMarkup(createElement(AssistantReply, {
      replyGroup: { rows: [{ message: generatedMessage, key: 'assistant-file', anchorKey: 'assistant-file', notes: [] }] },
      activeMirrorMsg: { ...generatedMessage, blocks: [peer, ...rawBlocks] },
      activitySourceBlocks: rawBlocks,
      useDbActivePayload: false,
      hasLivePayload: true,
      streamItems: [{
        type: 'tool', tool: 'Bash', tool_use_id: 'tool-pdf', status: 'done',
      }],
      chatId: 'chat-generated-file', isStreaming,
    }))
    assert.match(html, /Exchang(?:ing|ed) messages/i)
    assert.equal(html.includes('chat__attach-file'), !isStreaming)
  })
}

for (const isStreaming of [true, false]) {
  test(`generated image shows directly while docs remain deferred (${isStreaming})`, () => {
    const path = '/api/chats/chat-generated-file/generated-files/chart.png'
    const html = renderToStaticMarkup(createElement(MsgContent, {
      msg: {role: 'assistant', blocks: [{type: 'generated_files', files: [
        {name: 'chart.png', mime_type: 'image/png', previewable: true},
        {name: 'notes.pdf', mime_type: 'application/pdf', previewable: true},
      ]}], media_dimensions: {[path]: {width: 1536, height: 1024}}},
      chatId: 'chat-generated-file', isStreaming,
    }))
    assert.match(html, /--md-image-ratio:1536 \/ 1024/)
    assert.match(html, /Loading image/)
    assert.equal(html.includes('notes.pdf'), !isStreaming)
  })
}

test('an embedded generated image is not also rendered as an attachment', () => {
  const href = '/api/chats/chat-generated-file/generated-files/chart.png'
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: {role: 'assistant', blocks: [
      {type: 'text', content: `![chart](${href}?preview=true)`},
      {type: 'generated_files', files: [{name: 'chart.png', mime_type: 'image/png', previewable: true}]},
    ]}, chatId: 'chat-generated-file', isStreaming: false,
  }))
  assert.equal((html.match(/class="md-image-frame"/g) || []).length, 1)
  assert.doesNotMatch(html, /chat__generated-image/)
})

test('an image URL mentioned only in a code example does not hide the actual picture', () => {
  const href = '/api/chats/chat-generated-file/generated-files/chart.png'
  const html = renderToStaticMarkup(createElement(MsgContent, {
    msg: {role: 'assistant', blocks: [
      {type: 'text', content: '`![chart](' + href + ')`'},
      {type: 'generated_files', files: [{name: 'chart.png', mime_type: 'image/png', previewable: true}]},
    ]}, chatId: 'chat-generated-file', isStreaming: false,
  }))
  assert.match(html, /chat__generated-image/)
})
